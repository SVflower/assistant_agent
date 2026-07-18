"""RunCoordinator 状态转换与恢复测试。"""

from __future__ import annotations

import pytest

from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.agent.run_state import PermissionGrantState
from assistant_agent.llm.client import ToolCall
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.tools.base import ToolBudget, ToolContext, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest, PermissionScope


def _coordinator(tmp_path) -> RunCoordinator:
    return RunCoordinator.create(
        RunStore(tmp_path),
        task="test",
        provider="p",
        model="m",
        system_prompt="sys",
        tool_schemas=[],
        interactive=True,
        max_iterations=5,
        max_tool_calls=10,
        max_total_tool_output_chars=100,
        run_id="run-1",
    )


def _planned(coordinator: RunCoordinator) -> ToolCall:
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a"})
    messages = [
        {"role": "user", "content": "test"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                }
            ],
        },
    ]
    coordinator.model_completed(messages, [call])
    return call


def _request() -> PermissionRequest:
    return PermissionRequest("read_file", Capability.FILESYSTEM_READ, "a", "read")


def test_create_initialize_and_load(tmp_path):
    coordinator = _coordinator(tmp_path)
    budget = ToolBudget(max_calls=10, max_total_output_chars=100)
    coordinator.initialize([{"role": "user", "content": "test"}], None, budget)
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    assert loaded.state.messages[-1]["content"] == "test"
    assert loaded.state.phase == "model_pending"


def test_tool_lifecycle_checkpoints_each_transition(tmp_path):
    coordinator = _coordinator(tmp_path)
    _planned(coordinator)
    coordinator.approval_pending("c1", [_request()], "safe_readonly")
    assert RunCoordinator.load(RunStore(tmp_path), "run-1").state.phase == "awaiting_approval"
    coordinator.tool_started("c1", [_request()], "safe_readonly")
    assert RunCoordinator.load(RunStore(tmp_path), "run-1").state.tool_calls[0].status == "started"
    coordinator.tool_completed("c1", ToolResult.ok("done"), [_request()], "safe_readonly")
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    assert loaded.state.tool_calls[0].status == "completed"
    assert loaded.state.messages[-1]["tool_call_id"] == "c1"


def test_illegal_transition_is_rejected(tmp_path):
    coordinator = _coordinator(tmp_path)
    _planned(coordinator)
    coordinator.tool_started("c1", [_request()], "safe_readonly")
    with pytest.raises(ValueError, match="started"):
        coordinator.tool_started("c1", [_request()], "safe_readonly")


def test_started_load_becomes_uncertain_then_retry(tmp_path):
    coordinator = _coordinator(tmp_path)
    _planned(coordinator)
    coordinator.tool_started("c1", [_request()], "requires_decision")

    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    assert loaded.mark_uncertain_if_needed()[0].id == "c1"
    assert loaded.state.status == "paused"
    assert loaded.state.phase == "tool_uncertain"
    loaded.retry("c1")
    assert loaded.state.tool_calls[0].status == "planned"


def test_uncertain_skip_injects_stable_tool_result(tmp_path):
    coordinator = _coordinator(tmp_path)
    _planned(coordinator)
    coordinator.tool_started("c1", [_request()], "requires_decision")
    result = coordinator.skip("c1")
    assert result.code == "recovery_skipped"
    assert coordinator.state.tool_calls[0].status == "skipped"
    assert coordinator.state.messages[-1]["role"] == "tool"


def test_completed_call_cannot_be_replayed(tmp_path):
    coordinator = _coordinator(tmp_path)
    _planned(coordinator)
    coordinator.tool_started("c1", [_request()], "safe_readonly")
    coordinator.tool_completed("c1", ToolResult.ok("done"), [_request()], "safe_readonly")
    with pytest.raises(ValueError, match="started"):
        coordinator.retry("c1")


def test_normalize_empty_and_duplicate_call_ids(tmp_path):
    coordinator = _coordinator(tmp_path)
    coordinator.state.messages = [{"role": "assistant", "tool_calls": [{"id": "dup"}]}]
    calls = coordinator.normalize_tool_calls(
        [
            ToolCall(id="", name="a", arguments={}),
            ToolCall(id="dup", name="b", arguments={}),
            ToolCall(id="same", name="c", arguments={}),
            ToolCall(id="same", name="d", arguments={}),
        ]
    )
    ids = [call.id for call in calls]
    assert all(ids)
    assert len(ids) == len(set(ids))
    assert "dup" not in ids


def test_budget_and_exact_grants_restore(tmp_path):
    coordinator = _coordinator(tmp_path)
    coordinator.state.tool_budget.used_calls = 3
    coordinator.state.tool_budget.used_output_chars = 20
    coordinator.state.permission_grants = [
        PermissionGrantState(capability="filesystem.read", tool="read_file", target="a")
    ]
    ctx = ToolContext()
    budget = coordinator.restore_tool_context(ctx)
    assert budget.used_calls == 3 and budget.used_output_chars == 20
    assert ctx.permission_grants == {PermissionScope(Capability.FILESYSTEM_READ, "read_file", "a")}


def test_capture_permission_grants_is_stable(tmp_path):
    coordinator = _coordinator(tmp_path)
    ctx = ToolContext(
        permission_grants={
            PermissionScope(Capability.FILESYSTEM_WRITE, "write_file", "b"),
            PermissionScope(Capability.FILESYSTEM_READ, "read_file", "a"),
        }
    )
    coordinator.capture_permission_grants(ctx)
    assert [item.target for item in coordinator.state.permission_grants] == ["a", "b"]


def test_definition_differences_and_accept(tmp_path):
    coordinator = _coordinator(tmp_path)
    assert (
        coordinator.definition_differences(
            provider="p", model="m", system_prompt="sys", tool_schemas=[]
        )
        == []
    )
    differences = coordinator.definition_differences(
        provider="p2", model="m2", system_prompt="new", tool_schemas=[{"x": 1}]
    )
    assert {item.field for item in differences} == {
        "provider",
        "model",
        "system_prompt_hash",
        "tool_schema_hash",
    }
    coordinator.accept_definitions(
        provider="p2", model="m2", system_prompt="new", tool_schemas=[{"x": 1}]
    )
    assert coordinator.state.provider == "p2"


def test_cancel_is_terminal_and_cannot_resume(tmp_path):
    coordinator = _coordinator(tmp_path)
    coordinator.cancel("stopped", messages=[], compaction_checkpoint=None)
    assert coordinator.state.status == "cancelled"
    assert coordinator.state.phase == "terminal"
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    assert loaded.state.status == "cancelled"
