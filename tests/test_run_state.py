"""M10b RunState 严格契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant_agent.agent.run.state import (
    RunState,
    ToolBudgetState,
    ToolCallState,
    ToolResultState,
    canonical_hash,
    migrate_run_document,
    stable_call_id,
)


def _state(**overrides) -> RunState:
    data = {
        "run_id": "run-1",
        "session_id": None,
        "task": "test",
        "interactive": True,
        "provider": "test",
        "model": "openai/fake",
        "system_prompt_hash": "a" * 64,
        "tool_schema_hash": "b" * 64,
        "iteration_budget": 5,
        "tool_budget": ToolBudgetState(
            max_calls=10,
            max_total_output_chars=100,
            used_calls=0,
            used_output_chars=0,
        ),
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    data.update(overrides)
    return RunState.model_validate(data)


def _assistant_call(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }


def test_run_state_round_trip_is_strict_json():
    state = _state()
    restored = RunState.model_validate_json(state.model_dump_json())
    assert restored == state


def test_run_state_rejects_extra_and_coerced_fields():
    with pytest.raises(ValidationError):
        _state(iteration="1")
    with pytest.raises(ValidationError):
        _state(unknown=True)


def test_terminal_status_and_phase_must_match():
    with pytest.raises(ValidationError, match="terminal"):
        _state(status="completed")
    with pytest.raises(ValidationError, match="terminal"):
        _state(phase="terminal")


def test_tool_state_requires_assistant_call_and_result_pair():
    call = ToolCallState(id="c1", name="read_file", arguments={})
    with pytest.raises(ValidationError, match="assistant tool_call"):
        _state(tool_calls=[call])

    messages = [_assistant_call("c1")]
    assert _state(messages=messages, tool_calls=[call]).tool_calls[0].status == "planned"

    completed = call.model_copy(
        update={
            "status": "completed",
            "result": ToolResultState(output="ok", is_error=False, code="ok"),
        }
    )
    with pytest.raises(ValidationError, match="tool result"):
        _state(messages=messages, tool_calls=[completed])

    messages.append({"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"})
    assert _state(messages=messages, tool_calls=[completed]).tool_calls[0].result is not None


def test_tool_call_status_result_invariant():
    with pytest.raises(ValidationError, match="必须有结果"):
        ToolCallState(id="c", name="x", arguments={}, status="failed")
    with pytest.raises(ValidationError, match="不得带结果"):
        ToolCallState(
            id="c",
            name="x",
            arguments={},
            status="started",
            result=ToolResultState(output="x", is_error=False, code="ok"),
        )


def test_duplicate_current_call_ids_are_rejected():
    messages = [_assistant_call("c1")]
    calls = [
        ToolCallState(id="c1", name="a", arguments={}),
        ToolCallState(id="c1", name="b", arguments={}),
    ]
    with pytest.raises(ValidationError, match="重复 call ID"):
        _state(messages=messages, tool_calls=calls)


def test_repeat_count_requires_signature():
    with pytest.raises(ValidationError, match="last_signature"):
        _state(repeat_count=1)


def test_budget_usage_cannot_exceed_limit():
    with pytest.raises(ValidationError, match="超过上限"):
        ToolBudgetState(
            max_calls=1,
            max_total_output_chars=10,
            used_calls=2,
            used_output_chars=0,
        )


def test_stable_call_id_is_deterministic_and_position_sensitive():
    assert stable_call_id("r", 1, 2) == stable_call_id("r", 1, 2)
    assert stable_call_id("r", 1, 2) != stable_call_id("r", 1, 3)


def test_canonical_hash_ignores_mapping_order():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_unknown_schema_version_is_rejected():
    with pytest.raises(ValueError, match="schema_version"):
        migrate_run_document({"schema_version": 99})


def test_v1_document_migrates_and_cancelled_is_terminal():
    state = _state(status="cancelled", phase="terminal")
    assert state.schema_version == 7
    legacy = state.model_dump(mode="python")
    legacy["schema_version"] = 1
    legacy["status"] = "paused"
    legacy["phase"] = "model_pending"
    migrated = migrate_run_document(legacy)
    assert migrated["schema_version"] == 7
    assert migrated["retry_safety"] == "unknown"
    assert migrated["retry_baseline_available"] is False
    assert RunState.model_validate(migrated).status == "paused"
