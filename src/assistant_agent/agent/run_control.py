"""把进程内控制信号映射为 RunState 与 UI 事件。"""

from __future__ import annotations

from typing import Any

from assistant_agent.agent.events import StepEvent
from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.runtime import ControlState


def finish_control(
    state: ControlState,
    coordinator: RunCoordinator | None,
    *,
    messages: list[dict[str, Any]],
    compaction_checkpoint: dict[str, Any] | None,
    text: str | None = None,
) -> StepEvent:
    if state is ControlState.CANCEL_REQUESTED:
        message = text or "任务已强制取消；已发生的外部副作用不会自动回滚。"
        if coordinator is not None:
            coordinator.cancel(
                message,
                messages=messages,
                compaction_checkpoint=compaction_checkpoint,
            )
        return StepEvent(kind="interrupted", text=message)
    message = text or "任务已暂停，可使用 Run ID 恢复。"
    if coordinator is not None:
        coordinator.pause(message, messages=messages)
    return StepEvent(kind="interrupted", text=message)
