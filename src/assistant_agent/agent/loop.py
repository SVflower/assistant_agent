"""ReAct 主循环：观察、推理、调用工具，直到任务完成。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from assistant_agent.agent.artifact_capture import ArtifactCaptureWriter
from assistant_agent.agent.context.compaction import Compactor
from assistant_agent.agent.context.conversation import Conversation, estimate_tools_tokens
from assistant_agent.agent.context.window import ContextWindowError
from assistant_agent.agent.output_validation import OutputValidationError
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.agent.run.budgets import (
    BudgetContinueCheck,
    ContinuationController,
    budget_snapshot,
)
from assistant_agent.agent.run.control import finish_control
from assistant_agent.agent.run.coordinator import RecoveryChoice, RunCoordinator
from assistant_agent.agent.run.failures import budget_failure
from assistant_agent.agent.run.ports import ControlState, RunControlPort
from assistant_agent.agent.run.resume import resume_loop, sync_loop_state
from assistant_agent.agent.run.state import PendingOutputCaptureState, ToolCallState
from assistant_agent.agent.tool_batch import LoopCursor, execute_tool_batch
from assistant_agent.agent.turn import stream_model_turn
from assistant_agent.config.schema import AppConfig
from assistant_agent.contracts.attachments import UserMessageInputV1
from assistant_agent.contracts.events import StepEvent
from assistant_agent.contracts.failures import (
    BudgetResource,
    RunFailure,
)
from assistant_agent.contracts.outputs import OutputError, OutputInvalidError
from assistant_agent.providers.ports import ModelProviderPort
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.models import ToolBudget
from assistant_agent.tools.ports import ToolRegistryPort

_REPEAT_LIMIT = 3
RecoveryCheck = Callable[[ToolCallState], RecoveryChoice]


class AgentLoop:
    """驱动一次任务从开始到完成的 ReAct 循环。"""

    def __init__(
        self,
        config: AppConfig,
        client: ModelProviderPort,
        registry: ToolRegistryPort,
        tool_context: ToolContext,
        interactive: bool = True,
        interrupt_check: Callable[[], bool] | None = None,
        run_control: RunControlPort | None = None,
        continue_check: Callable[[int], bool] | None = None,
        budget_continue_check: BudgetContinueCheck | None = None,
        system_prompt: str | None = None,
        summary_client: ModelProviderPort | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._registry = registry
        self._tool_ctx = tool_context
        self._interrupt_check = interrupt_check
        self._run_control = run_control or tool_context.run_control
        self._tool_ctx.run_control = self._run_control
        self._continuation = ContinuationController(
            config.agent.continuation, budget_continue_check, continue_check
        )
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
            attachment_context_limit=min(
                config.attachments.max_context_tokens,
                int(
                    max(
                        config.agent.max_context_tokens
                        - config.agent.reserved_output_tokens
                        - tools_tokens,
                        0,
                    )
                    * config.attachments.max_context_ratio
                ),
            ),
            image_token_reserve=config.active_provider.unknown_image_token_reserve,
        )
        self._compaction = config.agent.compaction
        self._compactor: Compactor | None = None
        self._summary_follows_client = not bool(self._compaction.summary_model)
        if self._compaction.enabled:
            self._compactor = Compactor(
                summary_client or client,
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
        return self._control_state() is not ControlState.RUNNING

    def _control_state(self) -> ControlState:
        if self._interrupt_check is not None and self._interrupt_check():
            self._run_control.request_pause()
        return self._run_control.state

    def set_client(self, client: ModelProviderPort) -> None:
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

    def run(
        self,
        task: str | UserMessageInputV1,
        *,
        coordinator: RunCoordinator | None = None,
    ) -> Iterator[StepEvent]:
        """执行新任务；coordinator 为 None 时保持旧的非恢复路径。"""
        previous_budget = self._tool_ctx.budget
        budget = ToolBudget(
            max_calls=self._config.agent.max_tool_calls,
            max_total_output_chars=self._config.agent.max_total_tool_output_chars,
        )
        self._tool_ctx.budget = budget
        self._continuation.reset()
        if coordinator is not None:
            coordinator.bind_tool_context(self._tool_ctx)
        try:
            try:
                self._conversation.add_user(task)
            except ContextWindowError as exc:
                failure = budget_failure(
                    "context",
                    self._conversation.full_usage(),
                    self._config.agent.max_context_tokens,
                )
                if coordinator is not None:
                    coordinator.initialize(self.export_history(), self.export_checkpoint(), budget)
                    self._terminal(coordinator, False, failure.safe_message, failure=failure)
                yield StepEvent(kind="error", text=str(exc), is_error=True, failure=failure)
                return
            cursor = LoopCursor(0, self._config.agent.max_iterations)
            if coordinator is not None:
                task_text = task if isinstance(task, str) else task.content.safe_preview()
                if coordinator.state.task != task_text:
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
        yield from resume_loop(self, coordinator, recovery_check)

    def _drive(  # noqa: C901 - 单一循环保持模型/工具/捕获终态顺序可审计
        self, cursor: LoopCursor, coordinator: RunCoordinator | None
    ) -> Iterator[StepEvent]:
        budget = self._tool_ctx.budget
        if budget is None:
            raise RuntimeError("AgentLoop 缺少任务级 ToolBudget")

        while True:
            control_state = self._run_control.state
            if control_state is not ControlState.RUNNING:
                yield self._finish_control(control_state, coordinator)
                return
            if cursor.iteration >= cursor.iteration_budget:
                yield StepEvent(
                    kind="activity",
                    phase="waiting_interaction",
                    budget=budget_snapshot(cursor, budget),
                )
                new_limit = self._continuation.request(
                    "iterations",
                    used=cursor.iteration,
                    limit=cursor.iteration_budget,
                    budget=budget,
                    coordinator=coordinator,
                )
                if new_limit is None:
                    text = (
                        f"已达最大轮数（{cursor.iteration}），任务未完成。已执行的步骤见上方；"
                        "可用 --max-iterations 提高上限，或在 chat 中继续对话。"
                    )
                    failure = budget_failure(
                        "iterations", cursor.iteration, cursor.iteration_budget
                    )
                    self._terminal(coordinator, False, text, failure=failure)
                    yield StepEvent(kind="error", text=text, is_error=True, failure=failure)
                    return
                cursor.iteration_budget = new_limit
            cursor.iteration += 1
            yield from self._maybe_compact()
            yield StepEvent(
                kind="activity",
                phase="preparing_context",
                budget=budget_snapshot(cursor, budget),
            )

            try:
                request_messages = self._conversation.messages()
            except ContextWindowError as exc:
                failure = budget_failure(
                    "context",
                    self._conversation.full_usage(),
                    self._config.agent.max_context_tokens,
                )
                self._terminal(coordinator, False, failure.safe_message, failure=failure)
                yield StepEvent(
                    kind="error",
                    text=str(exc),
                    is_error=True,
                    failure=failure,
                )
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

            pending_output = (
                coordinator.state.pending_output_capture if coordinator is not None else None
            )
            capture_writer: ArtifactCaptureWriter | None = None
            if pending_output is not None:
                assert coordinator is not None
                try:
                    capture_writer = self._prepare_artifact_writer(coordinator, pending_output)
                except (OutputError, RuntimeError):
                    failure = self._output_capture_failure("输出草稿无法恢复。")
                    self._terminal(coordinator, False, failure.safe_message, failure=failure)
                    yield StepEvent(
                        kind="error", text=failure.safe_message, is_error=True, failure=failure
                    )
                    return

            yield StepEvent(
                kind="activity",
                phase="calling_model",
                budget=budget_snapshot(cursor, budget),
            )
            try:
                turn = yield from stream_model_turn(
                    self._client,
                    messages=request_messages,
                    tools=[] if pending_output is not None else self._tool_schemas,
                    control_state=self._control_state,
                    content_sink=capture_writer.write if capture_writer is not None else None,
                    emit_content=pending_output is None,
                    collect_content=pending_output is None,
                )
            except OutputError:
                failure = self._output_capture_failure("文件正文无效或超过输出限制。")
                self._terminal(coordinator, False, failure.safe_message, failure=failure)
                yield StepEvent(
                    kind="error", text=failure.safe_message, is_error=True, failure=failure
                )
                return
            content = turn.content
            tool_calls = turn.tool_calls
            stream_failure = turn.failure
            interrupted = turn.control_state
            if interrupted is not ControlState.RUNNING or stream_failure is not None:
                if content and pending_output is None:
                    self._conversation.add_assistant(content)
                if interrupted is not ControlState.RUNNING:
                    text = "已中断（用户请求停止）"
                    if interrupted is ControlState.CANCEL_REQUESTED:
                        yield self._finish_control(interrupted, coordinator, text=text)
                        return
                    if coordinator is not None:
                        coordinator.pause(
                            text,
                            messages=self.export_history(),
                            phase=(
                                "artifact_capture"
                                if pending_output is not None
                                else "model_pending"
                            ),
                        )
                    yield StepEvent(kind="interrupted", text=text)
                else:
                    assert stream_failure is not None
                    text = stream_failure.safe_message
                    self._terminal(coordinator, False, text, failure=stream_failure)
                    yield StepEvent(
                        kind="error",
                        text=text,
                        is_error=True,
                        failure=stream_failure,
                    )
                return

            if pending_output is not None:
                assert capture_writer is not None and coordinator is not None
                try:
                    event = self._finalize_artifact_capture(
                        coordinator, pending_output, capture_writer, tool_calls
                    )
                except OutputValidationError as exc:
                    feedback = (
                        f"输出验证失败 [{exc.reason_code}]：{exc}。"
                        "请重新输出完整文件正文，不要添加解释、Markdown 代码围栏或工具调用。"
                    )
                    self._conversation.replace_tool_result(
                        pending_output.call_id, "create_output", feedback
                    )
                    if coordinator.output_validation_failed(
                        pending_output.call_id,
                        exc.reason_code,
                        messages=self.export_history(),
                    ):
                        yield StepEvent(
                            kind="notice",
                            text="输出验证未通过，正在进行一次自动修复。",
                            result_code="output_validation_retrying",
                        )
                        continue
                    failure = self._output_capture_failure("文件正文连续两次未通过安全验证。")
                    self._terminal(coordinator, False, failure.safe_message, failure=failure)
                    yield StepEvent(
                        kind="error", text=failure.safe_message, is_error=True, failure=failure
                    )
                    return
                except OutputError:
                    failure = self._output_capture_failure("文件正文无效或超过输出限制。")
                    self._terminal(coordinator, False, failure.safe_message, failure=failure)
                    yield StepEvent(
                        kind="error", text=failure.safe_message, is_error=True, failure=failure
                    )
                    return
                yield event
                artifact = event.output
                if artifact is None:
                    raise RuntimeError("输出捕获完成事件缺少 Output Artifact")
                final = f"已生成文件：{artifact.filename}"
                self._conversation.add_assistant(final)
                self._terminal(coordinator, True, final)
                yield StepEvent(kind="final", text=final)
                return

            if not tool_calls:
                final = content or "（模型未返回内容）"
                self._conversation.add_assistant(final)
                self._terminal(coordinator, True, final)
                yield StepEvent(kind="final", text=final)
                return

            if coordinator is not None:
                tool_calls = coordinator.normalize_tool_calls(tool_calls)
            if any(call.name == "create_output" for call in tool_calls) and coordinator is None:
                failure = self._output_capture_failure("当前运行入口不支持受管输出捕获。")
                self._terminal(coordinator, False, failure.safe_message, failure=failure)
                yield StepEvent(
                    kind="error", text=failure.safe_message, is_error=True, failure=failure
                )
                return
            if any(call.name == "create_output" for call in tool_calls) and len(tool_calls) != 1:
                failure = self._output_capture_failure("create_output 必须作为单独的工具调用执行。")
                self._terminal(coordinator, False, failure.safe_message, failure=failure)
                yield StepEvent(
                    kind="error", text=failure.safe_message, is_error=True, failure=failure
                )
                return
            repeats = cursor.record_signature(tool_calls)
            if coordinator is not None:
                sync_loop_state(self, coordinator, cursor, budget)
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
                ensure_budget=lambda reason: self._continue_tool_budget(
                    reason, cursor, coordinator, budget
                ),
            )
            if outcome.control_state is not ControlState.RUNNING:
                if (
                    coordinator is not None
                    and outcome.control_state is ControlState.CANCEL_REQUESTED
                ):
                    coordinator.batch_completed(self.export_history())
                yield self._finish_control(outcome.control_state, coordinator)
                return
            if outcome.uncertain_call_id is not None:
                text = "工具执行结果未知，Run 已暂停，等待恢复决策。"
                if coordinator is not None:
                    coordinator.pause(text, phase="tool_uncertain")
                yield StepEvent(kind="interrupted", text=text)
                return
            if coordinator is not None:
                coordinator.batch_completed(self.export_history())
            if outcome.exhausted_reason is not None:
                yield StepEvent(
                    kind="activity",
                    phase="waiting_interaction",
                    budget=budget_snapshot(cursor, budget),
                )
                if not self._continue_tool_budget(
                    outcome.exhausted_reason, cursor, coordinator, budget
                ):
                    yield self._finish_budget_exhausted(
                        outcome.exhausted_reason, outcome.skipped_calls, coordinator
                    )
                    return

    @staticmethod
    def _output_capture_failure(message: str) -> RunFailure:
        return RunFailure(
            code="tool_failed",
            safe_message=message,
            retryable=True,
            allowed_actions=("retry_run", "stop"),
            phase="executing_tool",
            terminal_status="failed",
        )

    def _prepare_artifact_writer(
        self, coordinator: RunCoordinator, pending: PendingOutputCaptureState
    ) -> ArtifactCaptureWriter:
        output_store = self._tool_ctx.output_store
        session_id = self._tool_ctx.current_session_id
        if output_store is None or session_id is None:
            raise RuntimeError("输出存储未绑定")
        writer = ArtifactCaptureWriter(
            output_store,
            session_id=session_id,
            run_id=coordinator.run_id,
            pending=pending,
        )
        writer.start()
        return writer

    def _finalize_artifact_capture(
        self,
        coordinator: RunCoordinator,
        pending: PendingOutputCaptureState,
        writer: ArtifactCaptureWriter,
        tool_calls: list[Any],
    ) -> StepEvent:
        if tool_calls:
            raise OutputInvalidError("输出捕获轮禁止工具调用")
        artifact = writer.finalize()
        validation = writer.validation_result
        if validation is None:
            raise RuntimeError("输出捕获完成但缺少验证结果")
        coordinator.record_output_validation(
            pending.call_id, passed=True, result_code=validation.result_code
        )
        self._conversation.replace_tool_result(
            pending.call_id,
            "create_output",
            f"已创建输出文件：{artifact.filename}（{artifact.size_bytes} bytes）",
        )
        coordinator.output_capture_completed(artifact, messages=self.export_history())
        return StepEvent(
            kind="tool_result",
            tool_name="create_output",
            text=f"已创建输出文件：{artifact.filename}（{artifact.size_bytes} bytes）",
            call_id=pending.call_id,
            result_code="output_created",
            output=artifact,
        )

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
            resource: BudgetResource = "tool_calls"
        else:
            limit, used = budget.max_total_output_chars, budget.used_output_chars
            resource = "tool_output"
        self._tool_ctx.logger.budget_exhausted(
            reason=reason,
            limit=limit,
            used=used,
            skipped_calls=skipped_calls,
        )
        text = (
            "任务工具预算已耗尽，已停止后续执行。请缩小任务范围，或在配置中提高对应的 agent 预算。"
        )
        failure = budget_failure(resource, used, limit)
        self._terminal(coordinator, False, text, failure=failure)
        return StepEvent(kind="error", text=text, is_error=True, failure=failure)

    def _continue_tool_budget(
        self,
        reason: str,
        cursor: LoopCursor,
        coordinator: RunCoordinator | None,
        budget: ToolBudget | None,
    ) -> bool:
        if budget is None:
            return False
        resource: BudgetResource = "tool_calls" if reason == "max_tool_calls" else "tool_output"
        if resource == "tool_calls":
            used, limit = budget.used_calls, budget.max_calls
        else:
            used, limit = budget.used_output_chars, budget.max_total_output_chars
        return (
            self._continuation.request(
                resource=resource,
                used=used,
                limit=limit,
                budget=budget,
                coordinator=coordinator,
            )
            is not None
        )

    def _terminal(
        self,
        coordinator: RunCoordinator | None,
        success: bool,
        text: str,
        *,
        failure: RunFailure | None = None,
    ) -> None:
        if coordinator is None:
            return
        coordinator.capture_permission_grants(self._tool_ctx)
        coordinator.terminal(
            success=success,
            text=text,
            messages=self.export_history(),
            compaction_checkpoint=self.export_checkpoint(),
            failure=failure,
        )

    def _finish_control(
        self,
        state: ControlState,
        coordinator: RunCoordinator | None,
        *,
        text: str | None = None,
    ) -> StepEvent:
        return finish_control(
            state,
            coordinator,
            messages=self.export_history(),
            compaction_checkpoint=self.export_checkpoint(),
            text=text,
        )
