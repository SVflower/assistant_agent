"""ReAct 主循环：观察、推理、调用工具，直到任务完成。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from assistant_agent.agent.compaction import Compactor
from assistant_agent.agent.context import Conversation, estimate_tools_tokens
from assistant_agent.agent.events import StepEvent
from assistant_agent.agent.execution import LoopCursor, execute_tool_batch
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.agent.recovery import RecoveryChoice, RunCoordinator
from assistant_agent.agent.run_state import ToolCallState
from assistant_agent.agent.token_budget import ContextWindowError
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMClient, ToolCall
from assistant_agent.tools.base import ToolBudget, ToolContext
from assistant_agent.tools.registry import ToolRegistry

_REPEAT_LIMIT = 3
RecoveryCheck = Callable[[ToolCallState], RecoveryChoice]


class AgentLoop:
    """驱动一次任务从开始到完成的 ReAct 循环。"""

    def __init__(
        self,
        config: AppConfig,
        client: LLMClient,
        registry: ToolRegistry,
        tool_context: ToolContext,
        interactive: bool = True,
        interrupt_check: Callable[[], bool] | None = None,
        continue_check: Callable[[int], bool] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._registry = registry
        self._tool_ctx = tool_context
        self._interrupt_check = interrupt_check
        self._continue_check = continue_check
        self._tool_schemas = registry.schemas()
        tools_tokens = estimate_tools_tokens(self._tool_schemas)
        self._system_prompt = (
            build_system_prompt(interactive) if system_prompt is None else system_prompt
        )
        self._conversation = Conversation(
            max_history_messages=config.agent.max_history_messages,
            max_context_tokens=config.agent.max_context_tokens,
            interactive=interactive,
            system_prompt=self._system_prompt,
            tools_tokens=tools_tokens,
            reserved_output_tokens=config.agent.reserved_output_tokens,
        )
        self._compaction = config.agent.compaction
        self._compactor: Compactor | None = None
        self._summary_follows_client = not bool(self._compaction.summary_model)
        if self._compaction.enabled:
            summary_client = client
            if self._compaction.summary_model:
                provider = config.providers.get(self._compaction.summary_model)
                if provider is not None:
                    summary_client = LLMClient(provider)
            self._compactor = Compactor(
                summary_client,
                self._compaction.keep_recent_turns,
                self._compaction.summary_max_tokens,
            )

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        return self._tool_schemas

    def _interrupted(self) -> bool:
        return self._interrupt_check is not None and self._interrupt_check()

    def set_client(self, client: LLMClient) -> None:
        """替换模型客户端，同时保留对话与默认摘要模型跟随语义。"""
        self._client = client
        if self._compactor is not None and self._summary_follows_client:
            self._compactor.set_client(client)

    def set_interaction_available(self, available: bool) -> None:
        """更新当前进程是否可询问用户，不改变已保存的 system prompt 定义。"""
        self._tool_ctx.interactive = available

    def context_report(self) -> dict[str, int]:
        return self._conversation.budget_report()

    def export_history(self) -> list[dict[str, Any]]:
        return self._conversation.export_history()

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        self._conversation.load_history(messages)

    def export_checkpoint(self) -> dict[str, Any] | None:
        return self._conversation.get_checkpoint()

    def load_checkpoint(self, checkpoint: dict[str, Any] | None) -> None:
        self._conversation.load_checkpoint(checkpoint)

    def _maybe_compact(self) -> Iterator[StepEvent]:
        if self._compactor is None:
            return
        used = self._conversation.full_usage()
        if used <= self._conversation.budget() * self._compaction.threshold:
            return
        tail, base, previous = self._conversation.tail_after_checkpoint()
        result = self._compactor.compact(tail, base, previous)
        if result is None:
            return
        self._conversation.set_checkpoint(result.summary, result.covered_upto)
        if result.usage:
            yield StepEvent(kind="usage", usage=result.usage)
        yield StepEvent(
            kind="notice",
            text="（已把早前对话压缩为摘要，节省上下文；完整历史仍存档）",
        )

    def run(self, task: str, *, coordinator: RunCoordinator | None = None) -> Iterator[StepEvent]:
        """执行新任务；coordinator 为 None 时保持旧的非恢复路径。"""
        previous_budget = self._tool_ctx.budget
        budget = ToolBudget(
            max_calls=self._config.agent.max_tool_calls,
            max_total_output_chars=self._config.agent.max_total_tool_output_chars,
        )
        self._tool_ctx.budget = budget
        if coordinator is not None:
            coordinator.bind_tool_context(self._tool_ctx)
        try:
            try:
                self._conversation.add_user(task)
            except ContextWindowError as exc:
                yield StepEvent(kind="error", text=str(exc), is_error=True)
                return
            cursor = LoopCursor(0, self._config.agent.max_iterations)
            if coordinator is not None:
                if coordinator.state.task != task:
                    raise ValueError("coordinator task 与 run(task) 不一致")
                coordinator.initialize(self.export_history(), self.export_checkpoint(), budget)
            yield from self._drive(cursor, coordinator)
        finally:
            self._tool_ctx.budget = previous_budget

    def resume(
        self,
        coordinator: RunCoordinator,
        *,
        recovery_check: RecoveryCheck | None = None,
    ) -> Iterator[StepEvent]:
        """从 RunState 游标继续；不重新追加原始 task。"""
        previous_budget = self._tool_ctx.budget
        coordinator.restore_tool_context(self._tool_ctx)
        coordinator.bind_tool_context(self._tool_ctx)
        self.load_history(coordinator.state.messages)
        self.load_checkpoint(coordinator.state.compaction_checkpoint)
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
                else:
                    yield StepEvent(
                        kind="error",
                        text=coordinator.state.terminal_text,
                        is_error=True,
                    )
                return

            uncertain = coordinator.mark_uncertain_if_needed()
            for call in uncertain:
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
                    self._conversation.add_tool_result(call.id, call.name, result.output)
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
                    registry=self._registry,
                    ctx=self._tool_ctx,
                    conversation=self._conversation,
                    coordinator=coordinator,
                )
                coordinator.batch_completed(self.export_history())
                if outcome.exhausted_reason is not None:
                    yield self._finish_budget_exhausted(
                        outcome.exhausted_reason, outcome.skipped_calls, coordinator
                    )
                    return
            yield from self._drive(cursor, coordinator)
        finally:
            self._tool_ctx.budget = previous_budget

    def _drive(self, cursor: LoopCursor, coordinator: RunCoordinator | None) -> Iterator[StepEvent]:
        budget = self._tool_ctx.budget
        if budget is None:
            raise RuntimeError("AgentLoop 缺少任务级 ToolBudget")

        while True:
            if cursor.iteration >= cursor.iteration_budget:
                if self._continue_check is not None and self._continue_check(cursor.iteration):
                    cursor.iteration_budget += self._config.agent.max_iterations
                else:
                    text = (
                        f"已达最大轮数（{cursor.iteration}），任务未完成。已执行的步骤见上方；"
                        "可用 --max-iterations 提高上限，或在 chat 中继续对话。"
                    )
                    self._terminal(coordinator, False, text)
                    yield StepEvent(kind="error", text=text, is_error=True)
                    return
            cursor.iteration += 1
            yield from self._maybe_compact()

            try:
                request_messages = self._conversation.messages()
            except ContextWindowError as exc:
                text = str(exc)
                self._terminal(coordinator, False, text)
                yield StepEvent(kind="error", text=text, is_error=True)
                return
            if coordinator is not None:
                coordinator.before_model(
                    messages=self.export_history(),
                    compaction_checkpoint=self.export_checkpoint(),
                    iteration=cursor.iteration,
                    iteration_budget=cursor.iteration_budget,
                    last_signature=cursor.last_signature,
                    repeat_count=cursor.repeat_count,
                    budget=budget,
                )

            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            stream_error: str | None = None
            interrupted = False
            for event in self._client.complete_stream(
                messages=request_messages, tools=self._tool_schemas
            ):
                if event.kind == "reasoning":
                    yield StepEvent(kind="reasoning", text=event.text)
                elif event.kind == "content":
                    content_parts.append(event.text)
                    yield StepEvent(kind="content_delta", text=event.text)
                elif event.kind == "tool_calls":
                    tool_calls = event.tool_calls
                elif event.kind == "usage":
                    yield StepEvent(kind="usage", usage=event.usage)
                elif event.kind == "error":
                    stream_error = event.text
                if self._interrupted():
                    interrupted = True
                    break

            content = "".join(content_parts)
            if interrupted or stream_error is not None:
                if content:
                    self._conversation.add_assistant(content)
                text = "已中断（用户请求停止）" if interrupted else str(stream_error)
                if coordinator is not None:
                    coordinator.pause(
                        text,
                        messages=self.export_history(),
                        phase="model_pending",
                    )
                if interrupted:
                    yield StepEvent(kind="interrupted", text=text)
                else:
                    yield StepEvent(kind="error", text=text, is_error=True)
                return

            if not tool_calls:
                final = content or "（模型未返回内容）"
                self._conversation.add_assistant(final)
                self._terminal(coordinator, True, final)
                yield StepEvent(kind="final", text=final)
                return

            if coordinator is not None:
                tool_calls = coordinator.normalize_tool_calls(tool_calls)
            repeats = cursor.record_signature(tool_calls)
            if coordinator is not None:
                self._sync_coordinator(coordinator, cursor, budget)
            if repeats >= _REPEAT_LIMIT:
                text = f"检测到连续 {repeats} 次相同的工具调用，已停止以避免死循环。"
                self._terminal(coordinator, False, text)
                yield StepEvent(kind="error", text=text, is_error=True)
                return

            raw_tool_calls = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ]
            self._conversation.add_assistant(content or None, tool_calls=raw_tool_calls)
            if coordinator is not None:
                coordinator.model_completed(self.export_history(), tool_calls)

            if self._interrupted():
                text = "已中断；工具计划已保存，尚未执行。"
                if coordinator is not None:
                    coordinator.pause(text, phase="tools_pending")
                yield StepEvent(kind="interrupted", text=text)
                return

            outcome = yield from execute_tool_batch(
                tool_calls,
                registry=self._registry,
                ctx=self._tool_ctx,
                conversation=self._conversation,
                coordinator=coordinator,
            )
            if coordinator is not None:
                coordinator.batch_completed(self.export_history())
            if outcome.exhausted_reason is not None:
                yield self._finish_budget_exhausted(
                    outcome.exhausted_reason, outcome.skipped_calls, coordinator
                )
                return

    def _finish_budget_exhausted(
        self,
        reason: str,
        skipped_calls: int,
        coordinator: RunCoordinator | None,
    ) -> StepEvent:
        budget = self._tool_ctx.budget
        if budget is None:
            raise RuntimeError("AgentLoop 缺少任务级 ToolBudget")
        if reason == "max_tool_calls":
            limit, used = budget.max_calls, budget.used_calls
        else:
            limit, used = budget.max_total_output_chars, budget.used_output_chars
        self._tool_ctx.logger.budget_exhausted(
            reason=reason,
            limit=limit,
            used=used,
            skipped_calls=skipped_calls,
        )
        text = (
            "任务工具预算已耗尽，已停止后续执行。请缩小任务范围，或在配置中提高对应的 agent 预算。"
        )
        self._terminal(coordinator, False, text)
        return StepEvent(kind="error", text=text, is_error=True)

    def _sync_coordinator(
        self, coordinator: RunCoordinator, cursor: LoopCursor, budget: ToolBudget
    ) -> None:
        coordinator.sync_runtime(
            messages=self.export_history(),
            compaction_checkpoint=self.export_checkpoint(),
            iteration=cursor.iteration,
            iteration_budget=cursor.iteration_budget,
            last_signature=cursor.last_signature,
            repeat_count=cursor.repeat_count,
            budget=budget,
        )

    def _terminal(self, coordinator: RunCoordinator | None, success: bool, text: str) -> None:
        if coordinator is None:
            return
        coordinator.capture_permission_grants(self._tool_ctx)
        coordinator.terminal(
            success=success,
            text=text,
            messages=self.export_history(),
            compaction_checkpoint=self.export_checkpoint(),
        )
