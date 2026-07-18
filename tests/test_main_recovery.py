"""M10b CLI 与 Session/Run 协调测试。"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.main import app
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.service import sync_terminal_session
from assistant_agent.tools.base import ToolBudget


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


def test_terminal_run_rebuilds_missing_session_and_syncs_idempotently(tmp_path):
    coordinator = _terminal_coordinator(tmp_path)
    sessions = SessionStore(tmp_path / "sessions")

    first = sync_terminal_session(coordinator, sessions)
    second = sync_terminal_session(coordinator, sessions)

    assert first is not None and second is not None
    saved = sessions.load("session-1")
    assert saved.messages == [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "done"},
    ]
    loaded = RunCoordinator.load(RunStore(tmp_path / "runs"), "run-1")
    assert loaded.state.session_synced is True


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
