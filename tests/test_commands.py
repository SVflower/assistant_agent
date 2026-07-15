"""Slash 命令系统测试。用假 Console 捕获输出，无需真实终端/模型。"""

from __future__ import annotations

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.cli.commands import ChatContext, build_default_slash_registry
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import StreamEvent
from assistant_agent.mcp.configure import MCPProbeResult, MCPService
from assistant_agent.session.store import SessionStore
from assistant_agent.skills import SkillManager
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import build_default_registry


class FakeConsole:
    """捕获输出的假 Console（只实现命令用到的方法）。"""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.display_mode = "normal"
        self.model_label = ""

    def info(self, text: str) -> None:
        if self.display_mode != "quiet":
            self.out.append(text)

    def command_info(self, text: str) -> None:
        self.out.append(text)

    def error(self, text: str) -> None:
        self.out.append("ERROR:" + text)

    def print_sessions(self, metas) -> None:
        self.out.append(f"[sessions:{len(metas)}]")

    def ask_question(self, question, options) -> str:
        return options[0]

    def confirm(self, message):
        self.out.append("CONFIRM:" + message)
        return "allow"

    def set_display_mode(self, value, *, force=False) -> None:
        self.display_mode = value

    def set_model_label(self, model: str) -> None:
        self.model_label = model

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


def test_display_command_reports_and_switches_mode(tmp_path):
    ctx = _ctx(tmp_path)
    reg = build_default_slash_registry()
    reg.dispatch("/display", ctx)
    assert "normal" in ctx.console.text()
    reg.dispatch("/display verbose", ctx)
    assert ctx.console.display_mode == "verbose"
    assert ctx.config.ui.display_mode == "verbose"


def test_quiet_keeps_slash_command_feedback_visible(tmp_path):
    ctx = _ctx(tmp_path)
    reg = build_default_slash_registry()

    reg.dispatch("/display quiet", ctx)
    assert "展示模式已切换为 quiet" in ctx.console.text()
    ctx.console.out.clear()

    reg.dispatch("/display", ctx)
    reg.dispatch("/help", ctx)
    assert "当前展示模式：quiet" in ctx.console.text()
    assert "可用命令" in ctx.console.text()

    reg.dispatch("/display normal", ctx)
    assert ctx.console.display_mode == "normal"
    assert "展示模式已切换为 normal" in ctx.console.text()


def test_display_command_rejects_unknown_mode(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/display noisy", ctx)
    assert "未知展示模式" in ctx.console.text()


def test_mcp_empty(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/mcp", ctx)
    assert "未接入 MCP" in ctx.console.text()


def test_mcp_lists_servers(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.mcp_servers = [("web", ["nav", "click"]), ("db", ["query"])]
    build_default_slash_registry().dispatch("/mcp", ctx)
    out = ctx.console.text()
    assert "web（2 个工具）" in out and "nav, click" in out and "db（1 个工具）" in out


def test_skills_install_and_remove_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    ctx = _ctx(tmp_path)
    ctx.skill_manager = SkillManager(tmp_path / "workspace")
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\nbody\n", encoding="utf-8"
    )
    reg = build_default_slash_registry()

    reg.dispatch(f'/skills install "{source}" user', ctx)
    assert "已安装 Skill demo" in ctx.console.text()
    reg.dispatch("/skills remove demo user", ctx)
    assert "已卸载 Skill demo" in ctx.console.text()
    assert not (tmp_path / "home" / "skills" / "demo").exists()


def test_mcp_playwright_add_test_remove_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "active: p\nproviders:\n  p:\n    model: openai/test\n", encoding="utf-8"
    )
    ctx = _ctx(tmp_path)
    ctx.mcp_service = MCPService(config_path)
    monkeypatch.setattr(
        ctx.mcp_service,
        "probe",
        lambda name, server: MCPProbeResult(name, ("mcp__playwright__browser_navigate",), ()),
    )
    reg = build_default_slash_registry()

    reg.dispatch("/mcp add playwright user", ctx)
    assert "已验证并添加 playwright" in ctx.console.text()
    reg.dispatch("/mcp test playwright user", ctx)
    assert "验证通过" in ctx.console.text()
    artifact = tmp_path / "home" / "workspaces" / "placeholder" / "artifacts" / "mcp" / "playwright"
    monkeypatch.setattr(
        ctx.mcp_service,
        "purge_artifacts",
        lambda name: name == "playwright" and artifact.name == "playwright",
    )
    reg.dispatch("/mcp remove playwright user --purge-artifacts", ctx)
    assert "历史 artifact 保留" in ctx.console.text()
    assert "已清理历史 artifact" in ctx.console.text()


def test_model_switch_by_name(tmp_path):
    ctx = _ctx(tmp_path)
    build_default_slash_registry().dispatch("/model local", ctx)
    assert ctx.config.active == "local"
    assert ctx.session.provider == "local"
    assert ctx.session.model == "openai/b"
    assert ctx.console.model_label == "openai/b"
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
