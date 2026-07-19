"""Registry 工具生命周期边界测试。"""

from __future__ import annotations

from typing import Any

import pytest

from assistant_agent.tools.file_edit import WriteFileTool
from assistant_agent.tools.file_read import ReadFileTool
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.shell import ShellTool
from tests.support import Tool, ToolBudget, ToolContextFixture, ToolResult


class _Lifecycle:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, ReplayPolicy]] = []
        self.fail_at = ""

    def approval_pending(self, call_id, requests, replay_policy):
        self.events.append(("approval", call_id, replay_policy))
        if self.fail_at == "approval":
            raise RuntimeError("checkpoint failed")

    def tool_started(self, call_id, requests, replay_policy):
        self.events.append(("started", call_id, replay_policy))
        if self.fail_at == "started":
            raise RuntimeError("checkpoint failed")

    def tool_completed(self, call_id, result, requests, replay_policy):
        self.events.append(("completed", call_id, replay_policy))
        if self.fail_at == "completed":
            raise RuntimeError("checkpoint failed")


class _RecordingTool(Tool):
    name = "record"
    description = "test"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.seen_call_id = ""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    def permission_requests(self, args, ctx) -> list[PermissionRequest]:
        return []

    def run(self, args, ctx) -> ToolResult:
        self.events.append("run")
        self.seen_call_id = ctx.current_call_id
        return ToolResult.ok(args["value"])


def _registry(tool: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def test_lifecycle_started_and_completed_wrap_side_effect():
    run_events: list[str] = []
    tool = _RecordingTool(run_events)
    lifecycle = _Lifecycle()
    result = _registry(tool).execute(
        "record",
        {"value": "ok"},
        ToolContextFixture(),
        call_id="c1",
        lifecycle=lifecycle,
    )

    assert result.output == "ok"
    assert lifecycle.events == [
        ("started", "c1", "requires_decision"),
        ("completed", "c1", "requires_decision"),
    ]
    assert run_events == ["run"]
    assert tool.seen_call_id == "c1"


def test_approval_checkpoint_happens_before_prompt_and_started(tmp_path):
    lifecycle = _Lifecycle()

    def confirm(_message: str) -> str:
        assert lifecycle.events == [("approval", "c1", "requires_decision")]
        return "allow"

    target = tmp_path / "outside" / "x.txt"
    ctx = ToolContextFixture(workspace_root=tmp_path / "workspace", confirm=confirm)
    result = _registry(WriteFileTool()).execute(
        "write_file",
        {"path": str(target), "content": "x"},
        ctx,
        call_id="c1",
        lifecycle=lifecycle,
    )

    assert not result.is_error
    assert [item[0] for item in lifecycle.events] == ["approval", "started", "completed"]


def test_denied_call_completes_without_started(tmp_path):
    lifecycle = _Lifecycle()
    target = tmp_path / "outside.txt"
    result = _registry(WriteFileTool()).execute(
        "write_file",
        {"path": str(target), "content": "x"},
        ToolContextFixture(workspace_root=tmp_path / "workspace"),
        call_id="c1",
        lifecycle=lifecycle,
    )
    assert result.is_error and not result.executed
    assert [item[0] for item in lifecycle.events] == ["approval", "completed"]
    assert not target.exists()


def test_preflight_and_budget_failure_are_completed():
    lifecycle = _Lifecycle()
    tool = _RecordingTool([])
    registry = _registry(tool)
    invalid = registry.execute(
        "record",
        {},
        ToolContextFixture(),
        call_id="bad",
        lifecycle=lifecycle,
    )
    exhausted = registry.execute(
        "record",
        {"value": "x"},
        ToolContextFixture(budget=ToolBudget(max_calls=1, used_calls=1)),
        call_id="budget",
        lifecycle=lifecycle,
    )
    assert invalid.code == "invalid_arguments"
    assert exhausted.code == "budget_exhausted"
    assert [item[0] for item in lifecycle.events] == ["completed", "completed"]


def test_started_checkpoint_failure_prevents_tool_run():
    tool = _RecordingTool([])
    lifecycle = _Lifecycle()
    lifecycle.fail_at = "started"
    with pytest.raises(RuntimeError, match="checkpoint"):
        _registry(tool).execute(
            "record",
            {"value": "x"},
            ToolContextFixture(),
            call_id="c1",
            lifecycle=lifecycle,
        )
    assert tool.events == []


def test_completed_checkpoint_failure_propagates_after_tool_run():
    tool = _RecordingTool([])
    lifecycle = _Lifecycle()
    lifecycle.fail_at = "completed"
    with pytest.raises(RuntimeError, match="checkpoint"):
        _registry(tool).execute(
            "record",
            {"value": "x"},
            ToolContextFixture(),
            call_id="c1",
            lifecycle=lifecycle,
        )
    assert tool.events == ["run"]


def test_replay_policy_requires_workspace_read_or_trusted_readonly(tmp_path):
    inside = tmp_path / "inside.txt"
    inside.write_text("x", encoding="utf-8")
    lifecycle = _Lifecycle()
    _registry(ReadFileTool()).execute(
        "read_file",
        {"path": str(inside)},
        ToolContextFixture(workspace_root=tmp_path),
        call_id="read",
        lifecycle=lifecycle,
    )
    _registry(ShellTool()).execute(
        "run_shell",
        {"command": "pwd"},
        ToolContextFixture(workspace_root=tmp_path),
        call_id="shell",
        lifecycle=lifecycle,
    )
    policies = {
        call_id: policy for event, call_id, policy in lifecycle.events if event == "started"
    }
    assert policies == {"read": "safe_readonly", "shell": "safe_readonly"}


def test_lifecycle_requires_call_id_and_context_is_restored():
    tool = _RecordingTool([])
    ctx = ToolContextFixture(current_call_id="outer")
    with pytest.raises(ValueError, match="call_id"):
        _registry(tool).execute("record", {"value": "x"}, ctx, lifecycle=_Lifecycle())
    _registry(tool).execute("record", {"value": "x"}, ctx, call_id="inner")
    assert tool.seen_call_id == "inner"
    assert ctx.current_call_id == "outer"
