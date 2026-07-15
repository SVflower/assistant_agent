"""Runtime 装配失败时的资源回滚测试。"""

from __future__ import annotations

import pytest

from assistant_agent.cli import setup
from assistant_agent.config.schema import AppConfig, MCPConfig, MCPServerConfig
from assistant_agent.obs import NullLogger
from assistant_agent.skills import SkillMeta, SkillStore


class _Console:
    def error(self, _text):
        pass

    def info(self, _text):
        pass

    def confirm(self, _text):
        return "deny"

    def ask_question(self, _question, options):
        return options[0]

    def confirm_continue(self, _used):
        return False


class _Logger(NullLogger):
    def __init__(self):
        self.end_reasons = []

    def session_end(self, *, reason=""):
        self.end_reasons.append(reason)


class _MCP:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _PermissionContext:
    def __init__(self, allowed):
        self.allowed = allowed
        self.permission_grants = set()

    def request_permissions(self, _requests):
        return self.allowed


def test_build_runtime_rolls_back_mcp_and_logger(monkeypatch):
    config = AppConfig.model_validate({"active": "p", "providers": {"p": {"model": "openai/fake"}}})
    logger = _Logger()
    mcp = _MCP()
    monkeypatch.setattr(setup, "load_config", lambda _path: config)
    monkeypatch.setattr(setup, "create_logger", lambda *_args: logger)
    monkeypatch.setattr(setup, "_start_mcp", lambda *_args: mcp)
    monkeypatch.setattr(
        setup, "AgentLoop", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError, match="boom"):
        setup.build_runtime(None, _Console(), interactive=False, interrupt_check=lambda: False)

    assert mcp.closed is True
    assert logger.end_reasons == ["runtime_init_failed"]


def test_runtime_close_is_idempotent():
    config = AppConfig.model_validate({"active": "p", "providers": {"p": {"model": "x"}}})
    logger = _Logger()
    mcp = _MCP()
    runtime = setup.Runtime(
        config=config,
        loop=object(),  # type: ignore[arg-type]
        logger=logger,
        skill_store=SkillStore({}),
        mcp=mcp,  # type: ignore[arg-type]
    )
    runtime.close("done")
    runtime.close("again")
    assert mcp.closed is True
    assert logger.end_reasons == ["done"]


def test_authorize_skills_hides_untrusted_until_approved(tmp_path):
    trusted = SkillMeta("trusted", "trusted", tmp_path / "trusted.md", "personal", True)
    project = SkillMeta("project", "project", tmp_path / "project.md", "project", False)
    denied = setup._authorize_skills([project, trusted], _PermissionContext(False))
    approved_ctx = _PermissionContext(True)
    approved = setup._authorize_skills([project, trusted], approved_ctx)
    assert [meta.name for meta in denied] == ["trusted"]
    assert [meta.name for meta in approved] == ["project", "trusted"]
    assert approved_ctx.permission_grants


def test_start_mcp_warns_for_trusted_server(monkeypatch):
    messages = []

    class Console:
        def error(self, text):
            messages.append(text)

        def info(self, _text):
            pass

    class Manager:
        warnings = []

        def __init__(self, *_args):
            pass

        def start(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(setup, "MCPManager", Manager)
    config = MCPConfig(servers={"trusted": MCPServerConfig(command="fake", auto_approve=True)})
    setup._start_mcp(config, Console(), setup.build_default_registry())
    assert any("高风险" in message and "trusted" in message for message in messages)
