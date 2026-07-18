"""工具批次执行器与可持久化循环游标。"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from dataclasses import dataclass

from assistant_agent.agent.context import Conversation
from assistant_agent.agent.events import StepEvent
from assistant_agent.agent.failures import tool_failure
from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.llm.client import ToolCall
from assistant_agent.runtime import ControlState
from assistant_agent.tools.base import ToolContext, ToolResult
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
    control_state: ControlState = ControlState.RUNNING
    uncertain_call_id: str | None = None


def execute_tool_batch(
    calls: list[ToolCall],
    *,
    registry: ToolRegistry,
    ctx: ToolContext,
    conversation: Conversation,
    coordinator: RunCoordinator | None,
    ensure_budget: Callable[[str], bool] | None = None,
) -> Generator[StepEvent, None, BatchOutcome]:
    """顺序执行一个批次；已 checkpoint 的结果不重放。"""
    exhausted_reason: str | None = None
    skipped_calls = 0
    for index, call in enumerate(calls):
        control_state = ctx.run_control.state
        if control_state is not ControlState.RUNNING:
            if control_state is ControlState.CANCEL_REQUESTED:
                yield from _cancel_pending(calls[index:], conversation, coordinator)
            return BatchOutcome(exhausted_reason, skipped_calls, control_state)
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

        reason = ctx.budget.exhausted_reason() if ctx.budget is not None else None
        if reason is not None and ensure_budget is not None:
            yield StepEvent(kind="activity", phase="waiting_interaction")
            if ensure_budget(reason):
                exhausted_reason = None

        display = registry.display_call(call.name, call.arguments)
        yield StepEvent(
            kind="activity", phase="executing_tool", tool_name=call.name, display=display
        )
        yield StepEvent(
            kind="tool_call",
            tool_name=call.name,
            tool_args=call.arguments,
            call_id=call.id,
            display=display,
        )
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
        failure = tool_failure(result.code, retryable=result.retryable) if result.is_error else None
        if result.code != "mcp_outcome_unknown" or coordinator is None:
            conversation.add_tool_result(call.id, call.name, result.output)
        yield StepEvent(
            kind="tool_result",
            tool_name=call.name,
            text=result.output,
            is_error=result.is_error,
            call_id=call.id,
            display=registry.display_result(call.name, call.arguments, result),
            result_code=result.code,
            result_metadata=result.metadata,
            failure=failure,
        )
        if coordinator is not None:
            yield StepEvent(kind="activity", phase="saving_checkpoint")
        if result.code == "mcp_outcome_unknown" and coordinator is not None:
            return BatchOutcome(
                exhausted_reason,
                skipped_calls,
                uncertain_call_id=call.id,
            )
        control_state = ctx.run_control.state
        if control_state is not ControlState.RUNNING:
            if control_state is ControlState.CANCEL_REQUESTED:
                yield from _cancel_pending(calls[index + 1 :], conversation, coordinator)
            return BatchOutcome(exhausted_reason, skipped_calls, control_state)
    return BatchOutcome(exhausted_reason, skipped_calls)


def _cancel_pending(
    calls: list[ToolCall],
    conversation: Conversation,
    coordinator: RunCoordinator | None,
) -> Generator[StepEvent, None, None]:
    """为未开始的批次调用补稳定结果，保证 terminal 历史没有悬空 tool_call。"""
    for call in calls:
        result = ToolResult.error(
            "[cancelled] 任务已强制取消，工具未执行",
            code="cancelled",
            retryable=False,
            executed=False,
        )
        if coordinator is not None:
            coordinator.tool_completed(call.id, result, [], "requires_decision")
        conversation.add_tool_result(call.id, call.name, result.output)
        yield StepEvent(
            kind="tool_result",
            tool_name=call.name,
            text=result.output,
            is_error=True,
            call_id=call.id,
            result_code=result.code,
            failure=tool_failure(result.code, retryable=False),
        )
