"""公共 Runtime 装配与 CLI 适配器测试。"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
import typer

from assistant_agent.bootstrap import runtime as service_runtime
from assistant_agent.cli import setup
from assistant_agent.config.schema import AppConfig
from assistant_agent.integrations.skills import SkillStore
from assistant_agent.interaction import SafeDefaultInteractionPort
from assistant_agent.observability import NullLogger
from assistant_agent.persistence.attachments import AttachmentStore
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.service import AgentRuntime
from assistant_agent.tools.permissions import Capability, PermissionScope
from tests.support import ToolContextFixture


class _Console:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, text):
        self.errors.append(text)

    def info(self, _text):
        pass


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


def _config(path: Path) -> Path:
    path.write_text(
        "active: p\nproviders:\n  p:\n    model: openai/fake\n",
        encoding="utf-8",
    )
    return path


def test_cli_adapter_reports_missing_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    console = _Console()
    with pytest.raises(typer.Exit):
        setup.build_runtime(None, console, interactive=False)  # type: ignore[arg-type]
    assert any("未找到 config.yaml" in item for item in console.errors)


def test_public_runtime_rolls_back_mcp_and_logger(tmp_path, monkeypatch):
    config = AppConfig.model_validate({"active": "p", "providers": {"p": {"model": "x"}}})
    logger = _Logger()
    mcp = _MCP()
    processes = _MCP()
    monkeypatch.setattr(service_runtime, "load_config", lambda _path: config)
    monkeypatch.setattr(service_runtime, "create_logger", lambda *_args, **_kwargs: logger)
    monkeypatch.setattr(service_runtime, "ManagedProcessRegistry", lambda **_kwargs: processes)
    monkeypatch.setattr(
        service_runtime,
        "start_mcp",
        lambda *_args, **_kwargs: (mcp, []),
    )
    monkeypatch.setattr(
        service_runtime,
        "AgentLoop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(service_runtime.RuntimeInitializationError, match="loop"):
        service_runtime.create_runtime(
            config_path=_config(tmp_path / "config.yaml"),
            workspace_root=tmp_path,
            interactive=False,
        )
    assert mcp.closed is True
    assert processes.closed is True
    assert logger.end_reasons == ["runtime_init_failed"]


def test_runtime_close_is_idempotent(tmp_path):
    config = AppConfig.model_validate({"active": "p", "providers": {"p": {"model": "x"}}})
    logger = _Logger()
    mcp = _MCP()
    interaction = SafeDefaultInteractionPort()
    ctx = ToolContextFixture(interaction=interaction, workspace_root=tmp_path)
    ctx.permission_grants.add(
        PermissionScope(Capability.NETWORK_ACCESS, "controlled_public_web", "public-network")
    )
    ctx.always_allowed.add("network.access")
    runtime = AgentRuntime(
        config=config,
        loop=object(),  # type: ignore[arg-type]
        logger=logger,
        skill_store=SkillStore({}),
        tool_context=ctx,
        interaction=interaction,
        session_store=SessionStore(tmp_path / "sessions"),
        attachment_store=AttachmentStore(tmp_path / "attachments", config.attachments),
        run_store=RunStore(tmp_path / "runs"),
        execution_leases=FileSessionExecutionLeaseManager(tmp_path / "leases"),
        run_control=ctx.run_control,
        process_supervisor=ctx.process_supervisor,
        mcp=mcp,  # type: ignore[arg-type]
        workspace=ctx.workspace,
    )
    runtime.close("done")
    runtime.close("again")
    assert mcp.closed is True
    assert logger.end_reasons == ["done"]
    assert not ctx.permission_grants
    assert not ctx.always_allowed


def test_untrusted_skills_are_not_injected_and_return_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    project = tmp_path / ".agents" / "skills" / "project"
    project.mkdir(parents=True)
    (project / "SKILL.md").write_text(
        "---\nname: project\ndescription: project skill\n---\nbody",
        encoding="utf-8",
    )
    config = _config(tmp_path / "config.yaml")
    runtime = service_runtime.create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interactive=False,
    )
    try:
        assert runtime.visible_skills == []
        notice = next(item for item in runtime.notices if item.code == "skills_skipped_untrusted")
        assert notice.details["skills"] == ["project/project"]
    finally:
        runtime.close()


def test_runtime_startup_observer_reports_safe_order_and_cannot_break_startup(tmp_path):
    events = []

    def observe(event):
        events.append((event.phase, event.status))
        if event.phase == "discovering_skills":
            raise RuntimeError("UI observer failure")

    runtime = service_runtime.create_runtime(
        config_path=_config(tmp_path / "config.yaml"),
        workspace_root=tmp_path,
        interactive=False,
        startup_observer=observe,
    )
    try:
        started = [phase for phase, status in events if status == "started"]
        assert started == [
            "loading_config",
            "starting_workspace",
            "discovering_skills",
            "starting_web",
            "preparing_mcp",
            "creating_loop",
        ]
        assert events[-1] == ("ready", "completed")
        assert "inspect_runtime" in {item["function"]["name"] for item in runtime.loop.tool_schemas}
    finally:
        runtime.close()


def test_runtime_startup_observer_marks_failed_stage(tmp_path):
    events = []

    with pytest.raises(service_runtime.RuntimeConfigError):
        service_runtime.create_runtime(
            config_path=tmp_path / "missing.yaml",
            workspace_root=tmp_path,
            interactive=False,
            startup_observer=lambda event: events.append((event.phase, event.status)),
        )

    assert events == [("loading_config", "started"), ("loading_config", "failed")]


def test_runtime_registers_managed_process_when_context_allows(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "active: p\nproviders:\n  p:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 65536\n",
        encoding="utf-8",
    )
    runtime = service_runtime.create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interactive=False,
    )
    try:
        names = {item["function"]["name"] for item in runtime.loop.tool_schemas}
        assert "manage_process" in names
        assert runtime.process_manager is runtime.tool_context.process_manager
    finally:
        runtime.close()


def test_runtime_close_terminates_owned_background_process(tmp_path):
    marker = tmp_path / "runtime-process-survived.txt"
    code = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('alive')"
    parts = [sys.executable, "-c", code]
    command = subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)
    config = tmp_path / "config.yaml"
    config.write_text(
        "active: p\nproviders:\n  p:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 65536\n",
        encoding="utf-8",
    )
    runtime = service_runtime.create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interactive=False,
    )
    assert runtime.process_manager is not None
    runtime.process_manager.start(command, cwd=str(tmp_path))
    runtime.close()
    time.sleep(1.2)
    assert not marker.exists()
