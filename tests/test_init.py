"""init 向导测试。不触发真实终端/网络（endpoint mock、helper 单测）。"""

from __future__ import annotations

import pytest

from assistant_agent.cli import init as init_mod
from assistant_agent.cli.init import (
    check_env_var,
    generate_config,
    validate_generated,
    write_config,
)

# ---- generate_config：密钥脱敏是重点 ----


def test_generate_cloud_uses_env_var_not_key():
    cfg = generate_config("cloud", "openai/gpt-4o", env_var="OPENAI_API_KEY")
    prov = cfg["providers"]["cloud"]
    assert prov["model"] == "openai/gpt-4o"
    # 关键：只写 ${VAR}，绝不含明文 key
    assert prov["api_key"] == "${OPENAI_API_KEY}"
    assert cfg["active"] == "cloud"


def test_generate_cloud_with_custom_base():
    cfg = generate_config(
        "cloud", "openai/deepseek-v4", api_base="https://api.deepseek.com/v1",
        env_var="DEEPSEEK_API_KEY",
    )
    prov = cfg["providers"]["cloud"]
    assert prov["api_base"] == "https://api.deepseek.com/v1"
    assert prov["api_key"] == "${DEEPSEEK_API_KEY}"


def test_generate_anthropic():
    cfg = generate_config("anthropic", "anthropic/claude-sonnet-4-6", env_var="ANTHROPIC_API_KEY")
    prov = cfg["providers"]["anthropic"]
    assert prov["api_key"] == "${ANTHROPIC_API_KEY}"
    assert "api_base" not in prov


def test_model_prefix_autofilled_for_local():
    """裸模型名（如 lm_studio）自动补 openai/ 前缀，避免 LiteLLM 路由失败。"""
    cfg = generate_config("local", "lm_studio", api_base="http://localhost:1234/v1")
    assert cfg["providers"]["local"]["model"] == "openai/lm_studio"


def test_model_prefix_autofilled_for_cloud():
    cfg = generate_config("cloud", "gpt-4o", env_var="OPENAI_API_KEY")
    assert cfg["providers"]["cloud"]["model"] == "openai/gpt-4o"


def test_model_prefix_autofilled_for_anthropic():
    cfg = generate_config("anthropic", "claude-sonnet-4-6", env_var="ANTHROPIC_API_KEY")
    assert cfg["providers"]["anthropic"]["model"] == "anthropic/claude-sonnet-4-6"


def test_model_prefix_preserved_when_present():
    """已带前缀则不动。"""
    cfg = generate_config("local", "openai/local-model", api_base="http://x/v1")
    assert cfg["providers"]["local"]["model"] == "openai/local-model"


def test_generate_local_placeholder_key():
    cfg = generate_config("local", "openai/local-model", api_base="http://localhost:1234/v1")
    prov = cfg["providers"]["local"]
    assert prov["api_base"] == "http://localhost:1234/v1"
    # 本地占位 key（非机密），不是 ${VAR}
    assert prov["api_key"] == "lm-studio"


def test_generated_config_never_contains_real_key():
    """脱敏回归：生成物里不应出现 sk- 之类的明文。"""
    import yaml

    cfg = generate_config("cloud", "openai/gpt-4o", env_var="OPENAI_API_KEY")
    dumped = yaml.safe_dump(cfg, allow_unicode=True)
    assert "sk-" not in dumped
    assert "${OPENAI_API_KEY}" in dumped


# ---- write_config：已存在则备份 ----


def test_write_config_new(tmp_path):
    path = tmp_path / "config.yaml"
    backup = write_config(path, generate_config("local", "m", api_base="http://x/v1"))
    assert backup is None
    assert path.exists()


def test_write_config_backs_up_existing(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("old: 1\n", encoding="utf-8")
    backup = write_config(path, generate_config("local", "m", api_base="http://x/v1"))
    assert backup is not None and backup.exists()
    assert "old: 1" in backup.read_text(encoding="utf-8")  # 旧内容保留在备份


# ---- validate_generated ----


def test_validate_generated_ok(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path, generate_config("local", "openai/m", api_base="http://localhost:1234/v1"))
    ok, _ = validate_generated(path)
    assert ok


# ---- check_env_var ----


def test_mask_hides_secret():
    from assistant_agent.cli.init import _mask

    masked = _mask("sk-FAKEabcdef0123456789")
    assert masked == "sk-***"
    assert "FAKEabcdef" not in masked


def test_check_env_var(monkeypatch):
    monkeypatch.setenv("AA_TEST_KEY", "x")
    assert check_env_var("AA_TEST_KEY") is True
    monkeypatch.delenv("AA_TEST_KEY", raising=False)
    assert check_env_var("AA_TEST_KEY") is False


# ---- check_endpoint（mock httpx，不打真实网络）----


def test_check_endpoint_ok(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "a"}, {"id": "b"}]}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    ok, detail = init_mod.check_endpoint("http://localhost:1234/v1")
    assert ok
    assert "2 个模型" in detail


def test_check_endpoint_timeout(monkeypatch):
    import httpx

    def _raise(*a, **k):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr("httpx.get", _raise)
    ok, detail = init_mod.check_endpoint("http://localhost:1234/v1", timeout=1)
    assert not ok
    assert "超时" in detail


def test_check_endpoint_conn_fail(monkeypatch):
    def _raise(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr("httpx.get", _raise)
    ok, detail = init_mod.check_endpoint("http://localhost:9999/v1")
    assert not ok
    assert "连接失败" in detail


# ---- run_init：非交互环境直接拒绝 ----


def test_run_init_requires_tty(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    class _Console:
        def __init__(self):
            self.errors = []

        def error(self, t):
            self.errors.append(t)

        def info(self, t):
            pass

    c = _Console()
    code = init_mod.run_init(c, tmp_path / "config.yaml")
    assert code == 1
    assert any("交互式终端" in e for e in c.errors)


@pytest.mark.parametrize("kind", ["cloud", "anthropic", "local"])
def test_generate_all_kinds_validate(tmp_path, kind):
    """三种后端生成的配置都能通过 loader 校验。"""
    if kind == "local":
        cfg = generate_config(kind, "openai/m", api_base="http://localhost:1234/v1")
    else:
        cfg = generate_config(kind, "openai/m", env_var="OPENAI_API_KEY")
    path = tmp_path / "config.yaml"
    write_config(path, cfg)
    ok, detail = validate_generated(path)
    assert ok, detail
