"""Slash 命令系统测试。用假 Console 捕获输出，无需真实终端/模型。"""

from __future__ import annotations

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.cli.commands import ChatContext, build_default_slash_registry
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import StreamEvent
from assistant_agent.session.store import SessionStore
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import build_default_registry


class FakeConsole:
    """捕获输出的假 Console（只实现命令用到的方法）。"""

    def __init__(self) -> None:
        self.out: list[str] = []

    def info(self, text: str) -> None:
        self.out.append(text)

    def error(self, text: str) -> None:
        self.out.append("ERROR:" + text)

    def print_sessions(self, metas) -> None:
        self.out.append(f"[sessions:{len(metas)}]")

    def ask_question(self, question, options) -> str:
        return options[0]

    def text(self) -> str:
        return "\n".join(self.out)


class _FakeClient:
    def complete_stream(self, messages, tools=None):
        yield StreamEvent(kind="content", text="ok")


def _config(**providers) -> AppConfig:
    provs = providers or {"cloud": {"model": "openai/a"}, "local": {"model": "openai/b"}}
    return AppConfig.model_validate({"active": "cloud", "providers": provs})


def _ctx(tmp_path):
    config = _config()
    loop = AgentLoop(config, _FakeClient(), build_default_registry(), ToolContext())
    console = FakeConsole()
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = store.new_session(provider="cloud", model="openai/a")
    return ChatContext(config, loop, console, store, session)


def test_help_lists_commands(tmp_path):
    ctx = _ctx(tmp_path)
    reg = build_default_slash_registry()
    reg.dispatch("/help", ctx)
    out = ctx.console.text()
    for name in ("model", "sessions", "clear", "context", "exit"):
        assert f"/{name}" in out


def test_bare_slash_shows_help(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/", ctx)
    assert "可用命令" in ctx.console.text()


def test_unknown_command(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/bogus", ctx)
    assert "未知命令" in ctx.console.text()


def test_exit_sets_flag(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/exit", ctx)
    assert ctx.should_exit is True


def test_model_switch_by_name(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/model local", ctx)
    assert ctx.config.active == "local"
    assert "已切换到 local" in ctx.console.text()


def test_model_unknown_name(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/model nope", ctx)
    assert ctx.config.active == "cloud"  # 未切换
    assert "未知 provider" in ctx.console.text()


def test_clear_starts_new_session(tmp_path):
    ctx = _ctx(tmp_path)
    old_id = ctx.session.id
    # 先塞点历史
    ctx.loop.load_history([{"role": "user", "content": "hi"}])
    build_default_slash_registry().dispatch("/clear", ctx)
    assert ctx.session.id != old_id
    assert ctx.loop.export_history() == []


def test_context_reports_state(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.loop.load_history([{"role": "user", "content": "hi"}])
    build_default_slash_registry().dispatch("/context", ctx)
    assert "1 条消息" in ctx.console.text()


def test_sessions_lists(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.store.save(ctx.session, [{"role": "user", "content": "x"}])
    build_default_slash_registry().dispatch("/sessions", ctx)
    assert "[sessions:" in ctx.console.text()
