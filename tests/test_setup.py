"""Runtime 装配失败时的资源回滚测试。"""

from __future__ import annotations

import pytest

from assistant_agent.cli import setup
from assistant_agent.config.schema import AppConfig
from assistant_agent.obs import NullLogger


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
