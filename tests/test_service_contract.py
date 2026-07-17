"""公共事件和导入边界契约。"""

from assistant_agent.service import EVENT_CONTRACT_VERSION, StepEvent


def test_reasoning_is_always_marked_sensitive() -> None:
    event = StepEvent(kind="reasoning", text="hidden")
    assert event.contract_version == EVENT_CONTRACT_VERSION == 1
    assert event.sensitive is True


def test_existing_event_construction_remains_compatible() -> None:
    event = StepEvent(kind="tool_result", call_id="call-1")
    assert event.call_id == "call-1"
    assert event.terminal_status is None
    assert event.sensitive is False
