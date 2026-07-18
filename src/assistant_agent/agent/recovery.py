"""RunState 状态转换、checkpoint 与恢复决策。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from assistant_agent.agent.continuation import ContinuationStateMixin
from assistant_agent.agent.failures import tool_failure
from assistant_agent.agent.recovery_codec import (
    decode_budget,
    decode_result,
    encode_budget,
    encode_request,
    encode_result,
)
from assistant_agent.agent.recovery_definitions import DefinitionStateMixin
from assistant_agent.agent.run_state import (
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
from assistant_agent.contracts.failures import RunFailure
from assistant_agent.obs import NullLogger
from assistant_agent.providers.ports import ToolCall
from assistant_agent.session.run_store import LoadedRun, RunStore
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolBudget, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest, PermissionScope

RecoveryChoice = Literal["retry", "skip", "abort"]


class RunCoordinator(ContinuationStateMixin, DefinitionStateMixin):
    """把 Loop/Registry 的语义事件原子映射到 RunState。"""

    def __init__(
        self,
        store: RunStore,
        state: RunState,
        *,
        load_info: LoadedRun | None = None,
        logger: NullLogger | None = None,
    ):
        self.store = store
        self.state = state
        self.load_info = load_info
        self._logger = logger or NullLogger()
        self._tool_context: ToolContext | None = None

    @classmethod
    def create(
        cls,
        store: RunStore,
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
        logger: NullLogger | None = None,
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
        cls, store: RunStore, run_id: str, *, logger: NullLogger | None = None
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

    def bind_logger(self, logger: NullLogger) -> None:
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
            call.status = "started"
            call.result = None
            self.state.status = "paused"
            self.state.phase = "tool_uncertain"
            self.state.failure = tool_failure(result.code, retryable=False)
            self._capture_bound_context()
            self.checkpoint()
            return
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
            self.state.status = "paused"
            self.state.phase = "tool_uncertain"
            self.state.failure = tool_failure("mcp_outcome_unknown", retryable=False)
            self.checkpoint()
        return uncertain

    def retry(self, call_id: str) -> None:
        call = self._call(call_id)
        if call.status != "started":
            raise ValueError("只有 started 调用可重试")
        call.status = "planned"
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

    def call_state(self, call_id: str) -> ToolCallState:
        return self._call(call_id)

    def _call(self, call_id: str) -> ToolCallState:
        for call in self.state.tool_calls:
            if call.id == call_id:
                return call
        raise ValueError(f"当前批次不存在 call ID：{call_id}")
