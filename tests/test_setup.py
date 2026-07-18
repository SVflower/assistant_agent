"""公共 Runtime 装配与 CLI 适配器测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from assistant_agent.bootstrap import runtime as service_runtime
from assistant_agent.cli import setup
from assistant_agent.config.schema import AppConfig
from assistant_agent.interaction import SafeDefaultInteractionPort
from assistant_agent.obs import NullLogger
from assistant_agent.service.runtime import AgentRuntime
from assistant_agent.session.run_store import RunStore
from assistant_agent.session.store import SessionStore
from assistant_agent.skills import SkillStore
from assistant_agent.tools.base import ToolContext


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
    monkeypatch.setattr(service_runtime, "load_config", lambda _path: config)
    monkeypatch.setattr(service_runtime, "create_logger", lambda *_args, **_kwargs: logger)
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
    assert logger.end_reasons == ["runtime_init_failed"]


def test_runtime_close_is_idempotent(tmp_path):
    config = AppConfig.model_validate({"active": "p", "providers": {"p": {"model": "x"}}})
    logger = _Logger()
    mcp = _MCP()
    interaction = SafeDefaultInteractionPort()
    ctx = ToolContext(interaction=interaction, workspace_root=tmp_path)
    runtime = AgentRuntime(
        config=config,
        loop=object(),  # type: ignore[arg-type]
        logger=logger,
        skill_store=SkillStore({}),
        tool_context=ctx,
        interaction=interaction,
        session_store=SessionStore(tmp_path / "sessions"),
        run_store=RunStore(tmp_path / "runs"),
        run_control=ctx.run_control,
        process_supervisor=ctx.process_supervisor,
        mcp=mcp,  # type: ignore[arg-type]
        workspace=ctx.workspace,
    )
    runtime.close("done")
    runtime.close("again")
    assert mcp.closed is True
    assert logger.end_reasons == ["done"]


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
