"""工具批次执行器与可持久化循环游标。"""

from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass

from assistant_agent.agent.context import Conversation
from assistant_agent.agent.events import StepEvent
from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.llm.client import ToolCall
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry


@dataclass
class LoopCursor:
    iteration: int
    iteration_budget: int
    last_signature: str | None = None
    repeat_count: int = 0

    def record_signature(self, calls: list[ToolCall]) -> int:
        signature = json.dumps(
            [(call.name, call.arguments) for call in calls],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if signature == self.last_signature:
            self.repeat_count += 1
        else:
            self.last_signature = signature
            self.repeat_count = 1
        return self.repeat_count


@dataclass(frozen=True)
class BatchOutcome:
    exhausted_reason: str | None
    skipped_calls: int


def execute_tool_batch(
    calls: list[ToolCall],
    *,
    registry: ToolRegistry,
    ctx: ToolContext,
    conversation: Conversation,
    coordinator: RunCoordinator | None,
) -> Generator[StepEvent, None, BatchOutcome]:
    """顺序执行一个批次；已 checkpoint 的结果不重放。"""
    exhausted_reason: str | None = None
    skipped_calls = 0
    for call in calls:
        if coordinator is not None:
            saved = coordinator.result_for(call.id)
            if saved is not None:
                exhausted_reason = exhausted_reason or saved.budget_exhausted
                if not saved.executed:
                    skipped_calls += 1
                yield StepEvent(
                    kind="notice",
                    text=f"（恢复：工具 {call.name} 已有确认结果，跳过重放）",
                )
                continue

        yield StepEvent(kind="tool_call", tool_name=call.name, tool_args=call.arguments)
        result = registry.execute(
            call.name,
            call.arguments,
            ctx,
            call_id=call.id,
            lifecycle=coordinator,
        )
        if result.budget_exhausted is not None:
            exhausted_reason = exhausted_reason or result.budget_exhausted
        if not result.executed:
            skipped_calls += 1
        conversation.add_tool_result(call.id, call.name, result.output)
        yield StepEvent(
            kind="tool_result",
            tool_name=call.name,
            text=result.output,
            is_error=result.is_error,
        )
    return BatchOutcome(exhausted_reason, skipped_calls)
