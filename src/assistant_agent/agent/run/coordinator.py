"""RunState 状态转换、checkpoint 与恢复决策。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any, Literal

from assistant_agent.agent.run.budgets import ContinuationStateMixin
from assistant_agent.agent.run.checkpoint import (
    decode_budget,
    decode_result,
    encode_budget,
    encode_request,
    encode_result,
)
from assistant_agent.agent.run.failures import tool_failure
from assistant_agent.agent.run.ports import (
    LoadedRunPort,
    NullRunTelemetry,
    RunCheckpointRepository,
    RunTelemetry,
)
from assistant_agent.agent.run.recovery import DefinitionStateMixin
from assistant_agent.agent.run.state import (
    ContinuationBudgetState,
    PermissionGrantState,
    RunState,
    ToolBudgetState,
    ToolCallState,
    canonical_hash,
    migrate_run_document,
    new_run_id,
    now_iso,
    stable_call_id,
)
from assistant_agent.contracts.charts import (
    MAX_RUN_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACTS,
)
from assistant_agent.contracts.failures import RunFailure
from assistant_agent.providers.ports import ToolCall
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolBudget, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest, PermissionScope

RecoveryChoice = Literal["retry", "skip", "abort"]


class RunCoordinator(ContinuationStateMixin, DefinitionStateMixin):
    """把 Loop/Registry 的语义事件原子映射到 RunState。"""

    def __init__(
        self,
        store: RunCheckpointRepository,
        state: RunState,
        *,
        load_info: LoadedRunPort | None = None,
        logger: RunTelemetry | None = None,
    ):
        self.store = store
        self.state = state
        self.load_info = load_info
        self._logger = logger or NullRunTelemetry()
        self._tool_context: ToolContext | None = None

    @classmethod
    def create(
        cls,
        store: RunCheckpointRepository,
        *,
        task: str,
        provider: str,
        model: str,
        system_prompt: str,
        tool_schemas: list[dict[str, Any]],
        interactive: bool,
        max_iterations: int,
        max_tool_calls: int,
        max_total_tool_output_chars: int,
        continuation_max_extensions: int = 2,
        iteration_increment: int | None = None,
        max_iterations_hard: int | None = None,
        tool_call_increment: int | None = None,
        max_tool_calls_hard: int | None = None,
        tool_output_increment: int | None = None,
        max_tool_output_chars_hard: int | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        baseline_messages: list[dict[str, Any]] | None = None,
        baseline_compaction_checkpoint: dict[str, Any] | None = None,
        logger: RunTelemetry | None = None,
    ) -> RunCoordinator:
        timestamp = now_iso()
        state = RunState(
            run_id=run_id or new_run_id(),
            session_id=session_id,
            task=task,
            interactive=interactive,
            provider=provider,
            model=model,
            system_prompt_hash=canonical_hash(system_prompt),
            tool_schema_hash=canonical_hash(tool_schemas),
            baseline_messages=deepcopy(baseline_messages or []),
            baseline_compaction_checkpoint=deepcopy(baseline_compaction_checkpoint),
            retry_baseline_available=True,
            iteration_budget=max_iterations,
            tool_budget=ToolBudgetState(
                max_calls=max_tool_calls,
                max_total_output_chars=max_total_tool_output_chars,
                used_calls=0,
                used_output_chars=0,
            ),
            iteration_continuation=ContinuationBudgetState(
                resource="iterations",
                increment=iteration_increment or max_iterations,
                hard_limit=max_iterations_hard or max_iterations * 4,
                max_extensions=continuation_max_extensions,
            ),
            tool_call_continuation=ContinuationBudgetState(
                resource="tool_calls",
                increment=tool_call_increment or max_tool_calls,
                hard_limit=max_tool_calls_hard or max_tool_calls * 4,
                max_extensions=continuation_max_extensions,
            ),
            tool_output_continuation=ContinuationBudgetState(
                resource="tool_output",
                increment=tool_output_increment or max(max_total_tool_output_chars, 1),
                hard_limit=max_tool_output_chars_hard or max(max_total_tool_output_chars * 4, 1),
                max_extensions=continuation_max_extensions,
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return cls(store, state, logger=logger)

    @classmethod
    def load(
        cls,
        store: RunCheckpointRepository,
        run_id: str,
        *,
        logger: RunTelemetry | None = None,
    ) -> RunCoordinator:
        loaded = store.load(run_id)
        document = migrate_run_document(loaded.document)
        return cls(store, RunState.model_validate(document), load_info=loaded, logger=logger)

    @property
    def run_id(self) -> str:
        return self.state.run_id

    def checkpoint(self) -> None:
        self.state.updated_at = now_iso()
        validated = RunState.model_validate(self.state.model_dump(mode="python"))
        self.store.save(self.run_id, validated.model_dump(mode="json"))
        self.state = validated
        self._logger.run_checkpoint(
            run_id=self.run_id,
            status=self.state.status,
            phase=self.state.phase,
            iteration=self.state.iteration,
        )

    def bind_logger(self, logger: RunTelemetry) -> None:
        self._logger = logger

    def note_resume(self) -> None:
        source = self.load_info.source if self.load_info is not None else "current"
        warning = self.load_info.warning if self.load_info is not None else ""
        self._logger.run_resume(
            run_id=self.run_id,
            phase=self.state.phase,
            source=source,
            provider=self.state.provider,
            model=self.state.model,
            warning=warning,
        )

    def bind_tool_context(self, ctx: ToolContext) -> None:
        self._tool_context = ctx

    def _capture_bound_context(self) -> None:
        if self._tool_context is None:
            return
        self.capture_permission_grants(self._tool_context)
        if self._tool_context.budget is not None:
            self.state.tool_budget = encode_budget(self._tool_context.budget)

    def initialize(
        self,
        messages: list[dict[str, Any]],
        compaction_checkpoint: dict[str, Any] | None,
        budget: ToolBudget,
    ) -> None:
        self.state.messages = messages
        self.state.compaction_checkpoint = compaction_checkpoint
        self.state.tool_budget = encode_budget(budget)
        self.state.status = "running"
        self.state.phase = "model_pending"
        self._capture_bound_context()
        self._logger.run_start(
            run_id=self.run_id,
            provider=self.state.provider,
            model=self.state.model,
            task=self.state.task,
        )
        self.checkpoint()

    def sync_runtime(
        self,
        *,
        messages: list[dict[str, Any]],
        compaction_checkpoint: dict[str, Any] | None,
        iteration: int,
        iteration_budget: int,
        last_signature: str | None,
        repeat_count: int,
        budget: ToolBudget,
    ) -> None:
        self.state.messages = messages
        self.state.compaction_checkpoint = compaction_checkpoint
        self.state.iteration = iteration
        self.state.iteration_budget = iteration_budget
        self.state.last_signature = last_signature
        self.state.repeat_count = repeat_count
        self.state.tool_budget = encode_budget(budget)

    def before_model(self, **runtime: Any) -> None:
        self.sync_runtime(**runtime)
        self.state.status = "running"
        self.state.phase = "model_pending"
        self.state.tool_calls = []
        self._capture_bound_context()
        self.checkpoint()

    def normalize_tool_calls(self, calls: list[ToolCall]) -> list[ToolCall]:
        used = {
            str(raw_call.get("id"))
            for message in self.state.messages
            for raw_call in message.get("tool_calls") or []
            if isinstance(raw_call, dict) and raw_call.get("id")
        }
        normalized: list[ToolCall] = []
        for index, call in enumerate(calls):
            call_id = call.id.strip() if isinstance(call.id, str) else ""
            if not call_id or call_id in used:
                call_id = stable_call_id(self.run_id, self.state.iteration, index)
                suffix = 0
                candidate = call_id
                while candidate in used:
                    suffix += 1
                    candidate = f"{call_id}-{suffix}"
                call_id = candidate
            used.add(call_id)
            normalized.append(ToolCall(id=call_id, name=call.name, arguments=call.arguments))
        return normalized

    def model_completed(self, messages: list[dict[str, Any]], calls: list[ToolCall]) -> None:
        self.state.messages = messages
        self.state.tool_calls = [
            ToolCallState(id=call.id, name=call.name, arguments=call.arguments) for call in calls
        ]
        self.state.status = "running"
        self.state.phase = "tools_pending"
        self.checkpoint()

    def approval_pending(
        self,
        call_id: str,
        requests: Sequence[PermissionRequest],
        replay_policy: ReplayPolicy,
    ) -> None:
        call = self._call(call_id)
        if call.status not in {"planned", "awaiting_approval"}:
            raise ValueError(f"非法 approval 转换：{call.status}")
        call.status = "awaiting_approval"
        call.permission_requests = [encode_request(item) for item in requests]
        call.replay_policy = replay_policy
        self.state.phase = "awaiting_approval"
        self._capture_bound_context()
        self.checkpoint()

    def tool_started(
        self,
        call_id: str,
        requests: Sequence[PermissionRequest],
        replay_policy: ReplayPolicy,
    ) -> None:
        call = self._call(call_id)
        if call.status not in {"planned", "awaiting_approval"}:
            raise ValueError(f"非法 started 转换：{call.status}")
        call.status = "started"
        call.permission_requests = [encode_request(item) for item in requests]
        call.replay_policy = replay_policy
        self.state.phase = "tools_pending"
        self._capture_bound_context()
        self.checkpoint()

    def tool_completed(
        self,
        call_id: str,
        result: ToolResult,
        requests: Sequence[PermissionRequest],
        replay_policy: ReplayPolicy,
    ) -> None:
        call = self._call(call_id)
        if call.status not in {"planned", "awaiting_approval", "started"}:
            raise ValueError(f"非法 completed 转换：{call.status}")
        call.permission_requests = [encode_request(item) for item in requests]
        call.replay_policy = replay_policy
        if result.code == "mcp_outcome_unknown":
            if self.state.retry_safety == "safe":
                self.state.retry_safety = "uncertain"
            call.status = "started"
            call.result = None
            self.state.status = "paused"
            self.state.phase = "tool_uncertain"
            self.state.failure = tool_failure(result.code, retryable=False)
            self._capture_bound_context()
            self.checkpoint()
            return
        if result.chart is not None:
            self._record_presentation(result)
        if result.executed and replay_policy == "requires_decision":
            self.state.retry_safety = "unsafe"
        call.status = (
            "skipped" if not result.executed else ("failed" if result.is_error else "completed")
        )
        call.result = encode_result(result)
        self.state.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": result.output,
            }
        )
        self.state.phase = "tools_pending"
        self._capture_bound_context()
        self.checkpoint()

    def _record_presentation(self, result: ToolResult) -> None:
        artifact = result.chart
        if artifact is None:
            return
        existing = next(
            (item for item in self.state.presentations if item.artifact_id == artifact.artifact_id),
            None,
        )
        if existing is not None:
            if existing.content_hash == artifact.content_hash:
                result.chart = existing
                return
            self._reject_chart(result, "图表标识冲突，已保留原有图表。")
            return
        if (
            artifact.run_id != self.run_id
            or artifact.session_id != self.state.session_id
            or len(self.state.presentations) >= MAX_RUN_ARTIFACTS
            or sum(item.size_bytes for item in self.state.presentations) + artifact.size_bytes
            > MAX_RUN_ARTIFACT_BYTES
        ):
            self._reject_chart(result, "图表超过当前 Run 的安全存储上限，已忽略。")
            return
        self.state.presentations.append(artifact)

    @staticmethod
    def _reject_chart(result: ToolResult, message: str) -> None:
        result.output = message
        result.is_error = True
        result.code = "artifact_rejected"
        result.retryable = False
        result.executed = False
        result.chart = None

    def batch_completed(self, messages: list[dict[str, Any]]) -> None:
        if any(
            call.status not in {"completed", "failed", "skipped"} for call in self.state.tool_calls
        ):
            raise ValueError("工具批次尚未全部结束")
        self.state.messages = messages
        self.state.tool_calls = []
        self.state.phase = "model_pending"
        self.checkpoint()

    def terminal(
        self,
        *,
        success: bool,
        text: str,
        messages: list[dict[str, Any]],
        compaction_checkpoint: dict[str, Any] | None,
        failure: RunFailure | None = None,
    ) -> None:
        self.state.messages = messages
        self.state.compaction_checkpoint = compaction_checkpoint
        self.state.tool_calls = []
        self.state.status = "completed" if success else "failed"
        self.state.phase = "terminal"
        self.state.terminal_text = text
        self.state.failure = (
            None
            if success
            else (
                failure
                or RunFailure(
                    code="internal_error",
                    safe_message="任务执行失败。",
                    allowed_actions=("retry_run", "start_new_run"),
                    phase="saving_checkpoint",
                    terminal_status="failed",
                )
            )
        )
        if self.state.failure is not None and self.state.retry_safety != "safe":
            actions = tuple(
                action for action in self.state.failure.allowed_actions if action != "retry_run"
            )
            if self.state.retry_safety == "uncertain" and "resolve_uncertain_tool" not in actions:
                actions = (*actions, "resolve_uncertain_tool")
            self.state.failure = self.state.failure.model_copy(
                update={
                    "retryable": False,
                    "allowed_actions": actions or ("start_new_run",),
                    "unknown_side_effect": self.state.retry_safety == "uncertain",
                }
            )
        self.state.session_synced = self.state.session_id is None
        self._capture_bound_context()
        self.checkpoint()
        self._logger.run_end(
            run_id=self.run_id,
            status=self.state.status,
            reason=text,
        )

    def cancel(
        self,
        text: str,
        *,
        messages: list[dict[str, Any]],
        compaction_checkpoint: dict[str, Any] | None,
    ) -> None:
        """把强制取消保存为不可恢复 terminal Run。"""
        self.state.messages = messages
        self.state.compaction_checkpoint = compaction_checkpoint
        self.state.tool_calls = []
        self.state.status = "cancelled"
        self.state.phase = "terminal"
        self.state.terminal_text = text
        self.state.failure = None
        self.state.session_synced = self.state.session_id is None
        self._capture_bound_context()
        self.checkpoint()
        self._logger.run_end(run_id=self.run_id, status="cancelled", reason=text)

    def pause(
        self,
        text: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        phase: Literal["model_pending", "tools_pending", "tool_uncertain"] | None = None,
    ) -> None:
        if messages is not None:
            self.state.messages = messages
        self.state.status = "paused"
        if phase is not None:
            self.state.phase = phase
        self.state.terminal_text = text
        self._capture_bound_context()
        self.checkpoint()

    def mark_uncertain_if_needed(self) -> list[ToolCallState]:
        uncertain = [call for call in self.state.tool_calls if call.status == "started"]
        if uncertain:
            if self.state.retry_safety == "safe":
                self.state.retry_safety = "uncertain"
            self.state.status = "paused"
            self.state.phase = "tool_uncertain"
            self.state.failure = tool_failure("mcp_outcome_unknown", retryable=False)
            self.checkpoint()
        return uncertain

    def reconcile_orphan(self, request_hash: str) -> None:
        if self.state.status != "running":
            raise ValueError("只有 running Run 可以协调")
        self.state.reconciliation_request_hash = request_hash
        uncertain = self.mark_uncertain_if_needed()
        if not uncertain:
            self.pause("执行器已退出，Run 已安全暂停，等待恢复。")

    def prepare_retry(
        self,
        *,
        retry_of_run_id: str,
        idempotency_key_hash: str,
        request_hash: str,
    ) -> None:
        self.state.retry_of_run_id = retry_of_run_id
        self.state.retry_idempotency_key_hash = idempotency_key_hash
        self.state.retry_request_hash = request_hash

    def record_retry(self, idempotency_key_hash: str, new_run_id: str) -> None:
        existing = self.state.retry_requests.get(idempotency_key_hash)
        if existing is not None and existing != new_run_id:
            raise ValueError("幂等键已关联其他 Run")
        self.state.retry_requests[idempotency_key_hash] = new_run_id
        self.checkpoint()

    def remove_retry(self, idempotency_key_hash: str, expected_run_id: str) -> None:
        if self.state.retry_requests.get(idempotency_key_hash) != expected_run_id:
            return
        self.state.retry_requests.pop(idempotency_key_hash)
        self.checkpoint()

    def retry(self, call_id: str) -> None:
        call = self._call(call_id)
        if call.status != "started":
            raise ValueError("只有 started 调用可重试")
        call.status = "planned"
        if self.state.retry_safety == "uncertain" and call.replay_policy in {
            "safe_readonly",
            "safe_idempotent",
        }:
            other_started = [
                item
                for item in self.state.tool_calls
                if item.id != call_id and item.status == "started"
            ]
            if all(
                item.replay_policy in {"safe_readonly", "safe_idempotent"} for item in other_started
            ):
                self.state.retry_safety = "safe"
        self.state.status = "running"
        self.state.phase = "tools_pending"
        self.state.failure = None
        self.checkpoint()

    def skip(self, call_id: str) -> ToolResult:
        call = self._call(call_id)
        if call.status != "started":
            raise ValueError("只有 started 调用可跳过")
        result = ToolResult.error(
            "[recovery_skipped] 用户选择不重放执行结果未知的工具调用",
            code="recovery_skipped",
            executed=False,
        )
        call.status = "skipped"
        call.result = encode_result(result)
        self.state.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": result.output,
            }
        )
        self.state.status = "running"
        self.state.phase = "tools_pending"
        self.state.failure = None
        self.checkpoint()
        return result

    def restore_tool_context(self, ctx: ToolContext) -> ToolBudget:
        grants = {
            PermissionScope(Capability(item.capability), item.tool, item.target)
            for item in self.state.permission_grants
        }
        ctx.permission_grants = grants
        budget = decode_budget(self.state.tool_budget)
        ctx.budget = budget
        return budget

    def capture_permission_grants(self, ctx: ToolContext) -> None:
        self.state.permission_grants = sorted(
            (
                PermissionGrantState(
                    capability=scope.capability.value,
                    tool=scope.tool,
                    target=scope.target,
                )
                for scope in ctx.permission_grants
            ),
            key=lambda item: (item.capability, item.tool, item.target),
        )

    def mark_session_synced(self) -> None:
        if self.state.status not in {"cancelled", "completed", "failed"}:
            raise ValueError("非 terminal Run 不能标记 Session 已同步")
        self.state.session_synced = True
        self.checkpoint()

    def result_for(self, call_id: str) -> ToolResult | None:
        result = self._call(call_id).result
        return decode_result(result) if result is not None else None

    def count_tool_results(self, tool_name: str, marker: str) -> int:
        """从 checkpoint 消息账本统计安全标记，恢复后仍保持修正上限。"""
        return sum(
            1
            for message in self.state.messages
            if message.get("role") == "tool"
            and message.get("name") == tool_name
            and str(message.get("content", "")).startswith(marker)
        )

    def count_tool_results_matching(
        self,
        tool_name: str,
        marker: str,
        matches: Callable[[dict[str, Any]], bool],
    ) -> int:
        """按持久化调用参数隔离有界纠错链，不向 checkpoint 增加工具专属状态。"""
        arguments = self._persisted_tool_arguments()
        return sum(
            1
            for message in self.state.messages
            if message.get("role") == "tool"
            and message.get("name") == tool_name
            and str(message.get("content", "")).startswith(marker)
            and matches(arguments.get(str(message.get("tool_call_id")), {}))
        )

    def _persisted_tool_arguments(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for message in self.state.messages:
            for raw_call in message.get("tool_calls") or []:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    continue
                raw_arguments = function.get("arguments")
                try:
                    arguments = (
                        raw_arguments
                        if isinstance(raw_arguments, dict)
                        else json.loads(str(raw_arguments))
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                call_id = raw_call.get("id")
                if call_id and isinstance(arguments, dict):
                    result[str(call_id)] = arguments
        return result

    def call_state(self, call_id: str) -> ToolCallState:
        return self._call(call_id)

    def _call(self, call_id: str) -> ToolCallState:
        for call in self.state.tool_calls:
            if call.id == call_id:
                return call
        raise ValueError(f"当前批次不存在 call ID：{call_id}")
