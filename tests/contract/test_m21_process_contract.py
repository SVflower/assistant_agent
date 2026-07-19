"""M21 对服务调用方公开的向后兼容进程契约。"""

from __future__ import annotations

from assistant_agent.service import EVENT_CONTRACT_VERSION, StepEvent, ToolDisplay


def test_tool_display_timeout_is_additive_and_event_version_stays_v1():
    legacy = ToolDisplay("运行命令", "pytest")
    current = ToolDisplay("运行命令", "pytest", timeout_seconds=60)
    event = StepEvent(kind="tool_call", tool_name="run_shell", display=current)

    assert legacy.timeout_seconds is None
    assert current.timeout_seconds == 60
    assert event.contract_version == EVENT_CONTRACT_VERSION == 1
