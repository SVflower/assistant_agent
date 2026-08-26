"""公共事件和导入边界契约。"""

from assistant_agent.application.runtime import AgentRuntime as ApplicationRuntime
from assistant_agent.bootstrap.runtime import create_runtime as bootstrap_create_runtime
from assistant_agent.bootstrap.service import AgentService as BootstrapAgentService
from assistant_agent.service import (
    EVENT_CONTRACT_VERSION,
    AgentRuntime,
    AgentService,
    ItemEvent,
    create_runtime,
)


def test_reasoning_is_always_marked_sensitive() -> None:
    event = ItemEvent(kind="reasoning", text="hidden")
    assert event.contract_version == EVENT_CONTRACT_VERSION == 1
    assert event.sensitive is True


def test_event_defaults_are_stable() -> None:
    event = ItemEvent(kind="tool_result", call_id="call-1")
    assert event.call_id == "call-1"
    assert event.terminal_status is None
    assert event.sensitive is False


def test_service_facade_exports_canonical_implementations() -> None:
    assert AgentRuntime is ApplicationRuntime
    assert AgentService is BootstrapAgentService
    assert create_runtime is bootstrap_create_runtime
