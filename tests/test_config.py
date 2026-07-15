"""配置加载与校验测试。"""

from __future__ import annotations

import pytest

from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.schema import AppConfig

_VALID_YAML = """
active: cloud
providers:
  cloud:
    model: anthropic/claude-sonnet-4-6
    temperature: 0.5
  local:
    model: openai/local-model
    api_base: http://localhost:1234/v1
    api_key: lm-studio
"""


def _write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_valid_config(tmp_path):
    config = load_config(_write(tmp_path, _VALID_YAML))
    assert isinstance(config, AppConfig)
    assert config.active == "cloud"
    assert config.active_provider.model == "anthropic/claude-sonnet-4-6"
    assert config.active_provider.temperature == 0.5


def test_active_must_exist(tmp_path):
    bad = _VALID_YAML.replace("active: cloud", "active: nonexistent")
    with pytest.raises(ConfigError, match="nonexistent"):
        load_config(_write(tmp_path, bad))


def test_missing_file():
    with pytest.raises(ConfigError, match="不存在"):
        load_config("/no/such/config.yaml")


def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-123")
    yaml_text = _VALID_YAML + "    \n"
    yaml_text = """
active: cloud
providers:
  cloud:
    model: anthropic/claude-sonnet-4-6
    api_key: ${MY_KEY}
"""
    config = load_config(_write(tmp_path, yaml_text))
    assert config.active_provider.api_key == "secret-123"


def test_env_var_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    yaml_text = """
active: cloud
providers:
  cloud:
    model: openai/gpt-4o
    api_key: ${MISSING_KEY:-fallback}
"""
    config = load_config(_write(tmp_path, yaml_text))
    assert config.active_provider.api_key == "fallback"


def test_switching_provider_is_config_only(tmp_path):
    """核心卖点：切换 active 即切换后端，无需改代码。"""
    config = load_config(_write(tmp_path, _VALID_YAML))
    assert config.active_provider.model == "anthropic/claude-sonnet-4-6"

    switched = _VALID_YAML.replace("active: cloud", "active: local")
    config2 = load_config(_write(tmp_path, switched))
    assert config2.active_provider.model == "openai/local-model"
    assert config2.active_provider.api_base == "http://localhost:1234/v1"


def test_logging_config_defaults(tmp_path):
    """未配置 logging 时用安全默认值。"""
    config = load_config(_write(tmp_path, _VALID_YAML))
    assert config.logging.enabled is True
    assert config.logging.log_tool_io is True
    assert config.logging.dir == ".assistant_agent/logs"
    assert config.logging.max_payload_chars == 2000


def test_logging_config_override(tmp_path):
    yaml_text = (
        _VALID_YAML
        + """
logging:
  enabled: false
  log_tool_io: false
  max_payload_chars: 100
"""
    )
    config = load_config(_write(tmp_path, yaml_text))
    assert config.logging.enabled is False
    assert config.logging.log_tool_io is False
    assert config.logging.max_payload_chars == 100


def test_runtime_budget_defaults(tmp_path):
    config = load_config(_write(tmp_path, _VALID_YAML))
    assert config.tools.max_output_chars == 4000
    assert config.agent.max_tool_calls == 50
    assert config.agent.max_total_tool_output_chars == 50_000


def test_runtime_budget_override(tmp_path):
    yaml_text = (
        _VALID_YAML
        + """
agent:
  max_tool_calls: 7
  max_total_tool_output_chars: 9000
tools:
  max_output_chars: 500
"""
    )
    config = load_config(_write(tmp_path, yaml_text))
    assert config.tools.max_output_chars == 500
    assert config.agent.max_tool_calls == 7
    assert config.agent.max_total_tool_output_chars == 9000


def test_runtime_budget_rejects_invalid_values(tmp_path):
    yaml_text = (
        _VALID_YAML
        + """
agent:
  max_tool_calls: 0
tools:
  max_output_chars: -1
"""
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, yaml_text))


def test_permission_config_validates_mode_and_capability(tmp_path):
    invalid_mode = _VALID_YAML + "\npermissions:\n  mode: unsafe\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, invalid_mode))

    invalid_capability = (
        _VALID_YAML
        + "\npermissions:\n  rules:\n"
        + "    - effect: allow\n      capability: everything\n"
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, invalid_capability))


def test_legacy_tool_output_limit_migrates(tmp_path):
    config = load_config(
        _write(
            tmp_path,
            _VALID_YAML
            + """
agent:
  max_tool_output_chars: 600
""",
        )
    )
    assert config.tools.max_output_chars == 600
    assert not hasattr(config.agent, "max_tool_output_chars")


def test_new_tool_output_limit_wins_over_legacy(tmp_path):
    config = load_config(
        _write(
            tmp_path,
            _VALID_YAML
            + """
agent:
  max_tool_output_chars: 600
tools:
  max_output_chars: 700
""",
        )
    )
    assert config.tools.max_output_chars == 700


def test_summary_model_must_reference_provider(tmp_path):
    yaml_text = (
        _VALID_YAML
        + """
agent:
  compaction:
    enabled: true
    summary_model: missing
"""
    )
    with pytest.raises(ConfigError, match="summary_model"):
        load_config(_write(tmp_path, yaml_text))
