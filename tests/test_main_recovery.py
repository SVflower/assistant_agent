"""M10b CLI 与 Session/Run 协调测试。"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.config.paths import state_paths
from assistant_agent.main import app
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.service import sync_terminal_session
from tests.support import ToolBudget


def _terminal_coordinator(tmp_path, *, session_id="session-1") -> RunCoordinator:
    coordinator = RunCoordinator.create(
        RunStore(tmp_path / "runs"),
        task="task",
        provider="p",
        model="m",
        system_prompt="sys",
        tool_schemas=[],
        interactive=True,
        max_iterations=5,
        max_tool_calls=10,
        max_total_tool_output_chars=100,
        session_id=session_id,
        run_id="run-1",
    )
    messages = [{"role": "user", "content": "task"}]
    coordinator.initialize(messages, None, ToolBudget(max_calls=10))
    final_messages = [*messages, {"role": "assistant", "content": "done"}]
    coordinator.terminal(
        success=True,
        text="done",
        messages=final_messages,
        compaction_checkpoint=None,
    )
    return coordinator


def test_terminal_run_does_not_rebuild_missing_session(tmp_path):
    coordinator = _terminal_coordinator(tmp_path)
    sessions = SessionStore(tmp_path / "sessions")

    with pytest.raises(FileNotFoundError):
        sync_terminal_session(coordinator, sessions)
    assert not sessions._path("session-1").exists()
    loaded = RunCoordinator.load(RunStore(tmp_path / "runs"), "run-1")
    assert loaded.state.session_synced is False


def test_terminal_run_without_session_is_immediately_synced(tmp_path):
    coordinator = _terminal_coordinator(tmp_path, session_id=None)
    assert coordinator.state.session_synced is True
    assert sync_terminal_session(coordinator, SessionStore(tmp_path / "sessions")) is None


def test_runs_command_lists_checkpoint(tmp_path):
    runs_dir = tmp_path / "runs"
    coordinator = _terminal_coordinator(tmp_path)
    assert coordinator.store._dir == runs_dir
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "active: p",
                "providers:",
                "  p:",
                "    model: openai/fake",
                "agent:",
                "  recovery:",
                f'    dir: "{runs_dir.as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["runs", "--config", str(config)])
    assert result.exit_code == 0
    assert "run-1" in result.output
    assert "completed/terminal" in result.output


def test_sessions_delete_uses_service_lifecycle_and_force_cascade(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.yaml"
    config.write_text(
        "active: p\nproviders:\n  p:\n    model: openai/fake\n",
        encoding="utf-8",
    )
    paths = state_paths(tmp_path)
    lifecycle = paths.workspace / "session-lifecycle"
    sessions = SessionStore(paths.sessions, lifecycle_dir=lifecycle)
    runs = RunStore(paths.runs, lifecycle_dir=lifecycle)
    session = sessions.new_session(provider="p", model="openai/fake")
    sessions.save(session, [{"role": "user", "content": "active"}], must_exist=False)
    document = {
        "schema_version": 13,
        "run_id": "run-active",
        "session_id": session.id,
        "task": "active",
        "status": "running",
        "phase": "model_pending",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    runs.save("run-active", document)
    runs.save("run-active", document)
    lease = FileSessionExecutionLeaseManager(paths.workspace / "execution-leases").acquire(
        session.id
    )
    try:
        refused = CliRunner().invoke(
            app,
            ["sessions", "--config", str(config), "--delete", session.id],
            input="y\n",
        )
        assert refused.exit_code == 2
        assert "活跃 Run" in refused.output
        assert sessions.load(session.id).id == session.id
        assert runs.load("run-active").document["status"] == "running"

        deleted = CliRunner().invoke(
            app,
            [
                "sessions",
                "--config",
                str(config),
                "--delete",
                session.id,
                "--force",
            ],
            input="y\n",
        )
        assert deleted.exit_code == 0
        assert f"已删除会话 {session.id}" in deleted.output
    finally:
        lease.release()

    with pytest.raises(FileNotFoundError):
        sessions.load(session.id)
    with pytest.raises(FileNotFoundError):
        runs.load("run-active")
    with pytest.raises(FileNotFoundError):
        runs.save("run-active", document)
    assert not list(paths.runs.glob("run-active*.json"))


def test_chat_reports_current_session_when_exiting_after_clear(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(["active: p", "providers:", "  p:", "    model: openai/fake"]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["chat", "--config", str(config)],
        input="/clear\nexit\n",
    )

    assert result.exit_code == 0
    opened = re.search(r"新会话 (\d{8}-\d{6}-[0-9a-f]{8})", result.output)
    cleared = re.search(r"已开新会话 (\d{8}-\d{6}-[0-9a-f]{8})", result.output)
    closed = re.search(r"已结束会话 (\d{8}-\d{6}-[0-9a-f]{8})", result.output)
    assert opened is not None and cleared is not None and closed is not None
    assert opened.group(1) != cleared.group(1)
    assert closed.group(1) == cleared.group(1)
    assert f"assistant-agent chat --resume {cleared.group(1)}" in result.output


def test_chat_quiet_mode_keeps_slash_control_feedback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(["active: p", "providers:", "  p:", "    model: openai/fake"]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["chat", "--config", str(config)],
        input="/display quiet\n/display\n/help\n/display normal\nexit\n",
    )

    assert result.exit_code == 0
    assert "展示模式已切换为 quiet" in result.output
    assert "当前展示模式：quiet" in result.output
    assert "可用命令" in result.output
    assert "展示模式已切换为 normal" in result.output
