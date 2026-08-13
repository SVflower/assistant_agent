"""CLI Runtime 代际刷新测试；不启动真实 Provider 或 MCP 进程。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_agent.application.capabilities import RuntimePolicy
from assistant_agent.bootstrap.runtime import create_runtime
from assistant_agent.cli.reload import CLIRuntimeHolder
from assistant_agent.contracts.capabilities import (
    MCPServerCapability,
    RuntimeCapabilities,
    SkillCapability,
)
from assistant_agent.integrations.skills import SkillDiscoveryReport, SkillManager, SkillStore
from tests.test_commands import _ctx


class _Interaction:
    def __init__(self, pending: tuple[object, ...] = ()) -> None:
        self._pending = pending

    def pending_requests(self) -> tuple[object, ...]:
        return self._pending


class _MCP:
    def __init__(self, name: str, tool: str) -> None:
        self.name = name
        self.tool = tool
        self.closed = 0

    def server_summary(self) -> list[tuple[str, list[str]]]:
        return [(self.name, [self.tool])]

    def close(self) -> None:
        self.closed += 1


class _Runtime:
    def __init__(
        self,
        tmp_path,
        *,
        skill: str,
        mcp_name: str,
        mcp_tool: str,
        pending: tuple[object, ...] = (),
        report: SkillDiscoveryReport | None = None,
    ) -> None:
        context = _ctx(tmp_path)
        self.config = context.config
        self.loop = SimpleNamespace(name=f"loop-{skill}")
        self.logger = SimpleNamespace(name=f"logger-{skill}")
        self.session_store = context.store
        self.skill_store = SkillStore({}, report)
        self.visible_skills = [SimpleNamespace(name=skill, source="project", description=skill)]
        self.mcp = _MCP(mcp_name, mcp_tool)
        self.skill_manager = SimpleNamespace(name=f"skills-{skill}")
        self.mcp_service = SimpleNamespace(name=f"mcp-service-{skill}")
        self.tool_context = SimpleNamespace(name=f"tools-{skill}")
        self.interaction = _Interaction(pending)
        self.closed = 0
        self.capabilities = RuntimeCapabilities(
            sandbox="workspace",
            tools=("read_file", mcp_tool),
            skills=(SkillCapability(skill, "project", f"hash-{skill}"),),
            mcp_servers=(
                MCPServerCapability(
                    mcp_name,
                    "stdio",
                    "optional",
                    "connected",
                    (mcp_tool,),
                ),
            ),
            extension_management=True,
            profile="cli",
        )

    def capabilities_snapshot(self) -> RuntimeCapabilities:
        return self.capabilities

    def skills_meta(self) -> list[tuple[str, str]]:
        return [(item.name, f"[{item.source}] {item.description}") for item in self.visible_skills]

    def close(self, _reason: str = "") -> None:
        self.closed += 1
        self.mcp.close()


class _SessionRuntime:
    def __init__(self, runtime, session, *, active_run_id=None) -> None:
        self.runtime = runtime
        self.session = session
        self.active_run_id = active_run_id
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        self.runtime.close("session_runtime_closed")


def test_reload_atomically_swaps_capabilities_and_closes_old_runtime(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    old = _Runtime(tmp_path, skill="old", mcp_name="old", mcp_tool="mcp__old__read")
    new = _Runtime(
        tmp_path,
        skill="new",
        mcp_name="new",
        mcp_tool="mcp__new__read",
        report=SkillDiscoveryReport(("bad/SKILL.md",), ("shadowed",)),
    )
    old_session = _SessionRuntime(old, ctx.session)
    holder = CLIRuntimeHolder(old, old_session)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "assistant_agent.cli.reload.SessionRuntime",
        lambda runtime, session: _SessionRuntime(runtime, session),
    )

    message = holder.reload("all", ctx, lambda _control: new)  # type: ignore[arg-type]

    assert holder.runtime is new
    assert holder.generation == ctx.runtime_generation == 2
    assert ctx.skills[0][0] == "new"
    assert ctx.mcp_servers == [("new", ["mcp__new__read"])]
    assert new.capabilities.tools == ("read_file", "mcp__new__read")
    assert old.closed == old.mcp.closed == old_session.closed == 1
    assert "added=['new']" in message
    assert "invalid=['bad/SKILL.md']" in message
    assert "conflict=['shadowed']" in message


def test_reload_candidate_binding_failure_rolls_back_without_closing_old(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    old = _Runtime(tmp_path, skill="old", mcp_name="old", mcp_tool="mcp__old__read")
    candidate = _Runtime(tmp_path, skill="bad", mcp_name="bad", mcp_tool="mcp__bad__read")
    old_session = _SessionRuntime(old, ctx.session)
    holder = CLIRuntimeHolder(old, old_session)  # type: ignore[arg-type]

    def fail_session(_runtime, _session):
        raise RuntimeError("bind failed")

    monkeypatch.setattr("assistant_agent.cli.reload.SessionRuntime", fail_session)
    with pytest.raises(RuntimeError, match="bind failed"):
        holder.reload("all", ctx, lambda _control: candidate)  # type: ignore[arg-type]

    assert holder.runtime is old and holder.generation == 1
    assert old.closed == old_session.closed == 0
    assert candidate.closed == candidate.mcp.closed == 1


@pytest.mark.parametrize("blocked_by", ["run", "interaction"])
def test_reload_rejects_active_run_or_pending_interaction(tmp_path, blocked_by):
    ctx = _ctx(tmp_path)
    runtime = _Runtime(
        tmp_path,
        skill="old",
        mcp_name="old",
        mcp_tool="mcp__old__read",
        pending=(object(),) if blocked_by == "interaction" else (),
    )
    session_runtime = _SessionRuntime(
        runtime,
        ctx.session,
        active_run_id="run-1" if blocked_by == "run" else None,
    )
    holder = CLIRuntimeHolder(runtime, session_runtime)  # type: ignore[arg-type]
    called = False

    def factory(_control):
        nonlocal called
        called = True
        return runtime

    with pytest.raises(RuntimeError, match="拒绝刷新"):
        holder.reload("all", ctx, factory)
    assert called is False and holder.generation == 1 and runtime.closed == 0


def test_repeated_reload_is_idempotent_but_advances_generation(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    first = _Runtime(tmp_path, skill="same", mcp_name="same", mcp_tool="mcp__same__read")
    holder = CLIRuntimeHolder(first, _SessionRuntime(first, ctx.session))  # type: ignore[arg-type]
    monkeypatch.setattr(
        "assistant_agent.cli.reload.SessionRuntime",
        lambda runtime, session: _SessionRuntime(runtime, session),
    )
    second = _Runtime(tmp_path, skill="same", mcp_name="same", mcp_tool="mcp__same__read")
    third = _Runtime(tmp_path, skill="same", mcp_name="same", mcp_tool="mcp__same__read")

    first_message = holder.reload("all", ctx, lambda _control: second)  # type: ignore[arg-type]
    second_message = holder.reload("all", ctx, lambda _control: third)  # type: ignore[arg-type]

    assert holder.generation == 3
    assert "added=- · removed=- · updated=-" in first_message
    assert "added=- · removed=- · updated=-" in second_message
    assert first.closed == second.closed == 1 and third.closed == 0


def test_user_skill_install_is_visible_in_next_cli_runtime_generation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(home))
    config = tmp_path / "config.yaml"
    config.write_text(
        "active: p\nproviders:\n  p:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 65536\n",
        encoding="utf-8",
    )
    source = tmp_path / "source" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: demo\ndescription: dynamic skill\n---\nbody\n", encoding="utf-8"
    )
    first = create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interactive=True,
        runtime_policy=RuntimePolicy.cli(),
    )
    try:
        assert not first.capabilities.skills
        SkillManager(tmp_path).install(source, "user")
        second = create_runtime(
            config_path=config,
            workspace_root=tmp_path,
            interactive=True,
            runtime_policy=RuntimePolicy.cli(),
        )
        try:
            schemas = {item["function"]["name"] for item in second.loop.tool_schemas}
            assert "load_skill" in schemas
            assert [item.name for item in second.capabilities.skills] == ["demo"]
            assert "dynamic skill" in second.loop.system_prompt
        finally:
            second.close("test")
    finally:
        first.close("test")
