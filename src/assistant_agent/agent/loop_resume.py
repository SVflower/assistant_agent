"""AgentLoop 的 checkpoint 恢复驱动。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

from assistant_agent.agent.execution import LoopCursor, execute_tool_batch
from assistant_agent.agent.recovery import RecoveryChoice, RunCoordinator
from assistant_agent.agent.run_state import ToolCallState
from assistant_agent.contracts.events import StepEvent
from assistant_agent.providers.ports import ToolCall
from assistant_agent.runtime import ControlState
from assistant_agent.tools.models import ToolBudget


def sync_loop_state(
    loop: Any, coordinator: RunCoordinator, cursor: LoopCursor, budget: ToolBudget
) -> None:
    coordinator.sync_runtime(
        messages=loop.export_history(),
        compaction_checkpoint=loop.export_checkpoint(),
        iteration=cursor.iteration,
        iteration_budget=cursor.iteration_budget,
        last_signature=cursor.last_signature,
        repeat_count=cursor.repeat_count,
        budget=budget,
    )


def resume_loop(
    loop: Any,
    coordinator: RunCoordinator,
    recovery_check: Callable[[ToolCallState], RecoveryChoice] | None,
) -> Generator[StepEvent, None, None]:
    """恢复现有 Loop；状态转换仍全部委托 RunCoordinator。"""
    previous_budget = loop._tool_ctx.budget
    coordinator.restore_tool_context(loop._tool_ctx)
    coordinator.bind_tool_context(loop._tool_ctx)
    loop.load_history(coordinator.state.messages)
    loop.load_checkpoint(coordinator.state.compaction_checkpoint)
    cursor = LoopCursor(
        coordinator.state.iteration,
        coordinator.state.iteration_budget,
        coordinator.state.last_signature,
        coordinator.state.repeat_count,
    )
    try:
        if coordinator.state.phase == "terminal":
            if coordinator.state.status == "completed":
                yield StepEvent(kind="final", text=coordinator.state.terminal_text)
            elif coordinator.state.status == "cancelled":
                yield StepEvent(kind="interrupted", text=coordinator.state.terminal_text)
            else:
                yield StepEvent(
                    kind="error",
                    text=coordinator.state.terminal_text,
                    is_error=True,
                    failure=coordinator.state.failure,
                )
            return
        for call in coordinator.mark_uncertain_if_needed():
            if call.replay_policy == "safe_readonly":
                coordinator.retry(call.id)
                yield StepEvent(
                    kind="notice",
                    text=f"（恢复：自动重试只读工具 {call.name}，call_id={call.id}）",
                )
                continue
            if recovery_check is None:
                text = (
                    f"工具 {call.name} 的执行结果未知，非交互环境不会自动重放。"
                    f"请运行 assistant-agent resume {coordinator.run_id} 后选择。"
                )
                coordinator.pause(text, phase="tool_uncertain")
                yield StepEvent(kind="error", text=text, is_error=True)
                return
            choice = recovery_check(call)
            if choice == "abort":
                text = f"已暂停 Run {coordinator.run_id}，未重放 {call.name}。"
                coordinator.pause(text, phase="tool_uncertain")
                yield StepEvent(kind="interrupted", text=text)
                return
            if choice == "retry":
                coordinator.retry(call.id)
            else:
                result = coordinator.skip(call.id)
                loop._conversation.add_tool_result(call.id, call.name, result.output)
                yield StepEvent(
                    kind="tool_result",
                    tool_name=call.name,
                    text=result.output,
                    is_error=True,
                )
        if coordinator.state.tool_calls:
            calls = [
                ToolCall(id=call.id, name=call.name, arguments=call.arguments)
                for call in coordinator.state.tool_calls
            ]
            outcome = yield from execute_tool_batch(
                calls,
                registry=loop._registry,
                ctx=loop._tool_ctx,
                conversation=loop._conversation,
                coordinator=coordinator,
                ensure_budget=lambda reason: loop._continue_tool_budget(
                    reason, cursor, coordinator, loop._tool_ctx.budget
                ),
            )
            if outcome.control_state is not ControlState.RUNNING:
                if outcome.control_state is ControlState.CANCEL_REQUESTED:
                    coordinator.batch_completed(loop.export_history())
                yield loop._finish_control(outcome.control_state, coordinator)
                return
            if outcome.uncertain_call_id is not None:
                text = "工具执行结果未知，Run 已暂停，等待恢复决策。"
                coordinator.pause(text, phase="tool_uncertain")
                yield StepEvent(kind="interrupted", text=text)
                return
            coordinator.batch_completed(loop.export_history())
            if outcome.exhausted_reason is not None:
                yield StepEvent(kind="activity", phase="waiting_interaction")
                if not loop._continue_tool_budget(
                    outcome.exhausted_reason, cursor, coordinator, loop._tool_ctx.budget
                ):
                    yield loop._finish_budget_exhausted(
                        outcome.exhausted_reason, outcome.skipped_calls, coordinator
                    )
                    return
        yield from loop._drive(cursor, coordinator)
    finally:
        loop._tool_ctx.budget = previous_budget
