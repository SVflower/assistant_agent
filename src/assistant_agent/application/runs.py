"""Session/Run 的公共同步服务门面。"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal, cast

from assistant_agent.agent.run.coordinator import RecoveryChoice as LoopRecoveryChoice
from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.agent.run.ports import ControlState
from assistant_agent.agent.run.recovery import DefinitionDifference
from assistant_agent.agent.run.state import RunState, ToolCallState, canonical_hash
from assistant_agent.application.models import RunMeta, RunResumeInfo, Session
from assistant_agent.application.ports import (
    RunCatalogRepository,
    SessionExecutionLease,
    SessionRepository,
)
from assistant_agent.application.runtime import AgentRuntime
from assistant_agent.contracts.attachments import (
    AttachmentPayloadV1,
    AttachmentRefV1,
    AttachmentSummaryV1,
    AttachmentUploadV1,
    UserMessageInputV1,
    attachment_token_estimate,
    parse_message_content,
)
from assistant_agent.contracts.capabilities import RuntimeCapabilities
from assistant_agent.contracts.charts import (
    AssistantMessageSnapshot,
    ChartArtifactV2,
    PendingInteractionSnapshot,
    PresentationArtifactRefV2,
    RunSnapshot,
    stable_message_id,
)
from assistant_agent.contracts.errors import (
    AgentServiceError,
    ArtifactNotFoundError,
    AttachmentContextTooLargeError,
    AttachmentInvalidError,
    IdempotencyConflictError,
    InvalidForkRequestError,
    InvalidIdempotencyKeyError,
    RunNotFoundError,
    RunNotReconcilableError,
    RunNotResumableError,
    RunNotRetryableError,
    RunRecoveryRequiredError,
    RuntimeClosedError,
    SessionBusyError,
    SessionMigrationRequiredError,
    SessionNotFoundError,
    SessionRunConflictError,
    SessionUnavailableError,
    UnsupportedInputModalityError,
    UserMessageNotFoundError,
)
from assistant_agent.contracts.events import StepEvent, TerminalStatus
from assistant_agent.contracts.failures import AllowedAction, BudgetSnapshot, RunFailure
from assistant_agent.contracts.interactions import (
    DefinitionChangeRequest,
    DefinitionDifferenceInfo,
    RecoveryRequest,
)
from assistant_agent.contracts.sessions import PublicMessageSnapshot, SessionSnapshot

_PUBLIC_MESSAGE_ID = re.compile(r"^msg_[a-f0-9]{24}$")


class _ExecutionEvents(Iterator[StepEvent]):
    """为底层事件 generator 增加取消、关闭和最终收口语义。

    generator 中的异常是在调用 ``next()`` 时发生的，因此仅在 ``start_run`` 外包一层 try/except
    不够。本包装器保证正常耗尽、迭代异常和调用方提前 close 都只执行一次 ``on_close``，让 lease
    释放与 Session 同步不会被遗漏或重复。
    """

    def __init__(
        self,
        source: Iterator[StepEvent],
        on_close: Callable[[bool], None],
        request_cancel: Callable[[], object],
    ) -> None:
        self._source = source
        self._on_close = on_close
        self._request_cancel = request_cancel
        self._lock = threading.Lock()
        self._started = False
        self._iterating = False
        self._close_requested = False
        self._closing = False
        self._finished = False

    def __iter__(self) -> _ExecutionEvents:
        return self

    def __next__(self) -> StepEvent:
        # Python generator 不支持并发 next()。显式拒绝比让底层抛出难以归因的
        # "generator already executing" 更容易让 API 找到错误用法。
        with self._lock:
            if self._finished or self._close_requested:
                raise StopIteration
            if self._iterating:
                raise RuntimeError("RunExecution events 不支持并发迭代")
            self._started = True
            self._iterating = True
        try:
            item = next(self._source)
        except BaseException:
            with self._lock:
                self._iterating = False
            self._finish()
            raise
        with self._lock:
            self._iterating = False
            should_close = self._claim_close_locked()
        if should_close:
            self._close_source()
        return item

    def close(self) -> None:
        with self._lock:
            if self._finished:
                return
        self._request_cancel()
        with self._lock:
            self._close_requested = True
            should_close = self._claim_close_locked()
        if should_close:
            self._close_source()

    def _claim_close_locked(self) -> bool:
        if self._finished or not self._close_requested or self._iterating or self._closing:
            return False
        self._closing = True
        return True

    def _close_source(self) -> None:
        close = getattr(self._source, "close", None)
        try:
            if callable(close):
                close()
        except BaseException:
            with self._lock:
                self._closing = False
            raise
        self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            started = self._started
        self._on_close(started)


@dataclass(frozen=True)
class RunExecution:
    run_id: str
    events: Iterator[StepEvent]
    warning: str = ""

    def close(self) -> None:
        close = getattr(self.events, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> RunExecution:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class RetryRunExecution:
    original_run_id: str
    new_run_id: str
    created: bool
    events: Iterator[StepEvent]
    warning: str = ""

    def close(self) -> None:
        close = getattr(self.events, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> RetryRunExecution:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class _FinalizationResult:
    status: TerminalStatus
    notice_codes: tuple[str, ...] = ()


def inspect_run(store: RunCatalogRepository, run_id: str) -> RunResumeInfo:
    document = store.load(run_id).document
    return RunResumeInfo(
        run_id=run_id,
        session_id=document.get("session_id"),
        provider=str(document.get("provider", "")),
        interactive=bool(document.get("interactive", False)),
        created_at=str(document.get("created_at", "")),
        updated_at=str(document.get("updated_at", "")),
    )


def resume_standalone_run(runtime: AgentRuntime, run_id: str) -> RunExecution:
    """恢复 M10b 遗留的无 Session Run；新服务调用始终使用 SessionRuntime。"""
    runtime.run_control.reset()
    coordinator = RunCoordinator.load(runtime.run_store, run_id, logger=runtime.logger)
    if coordinator.state.session_id is not None:
        raise SessionRunConflictError("有 Session 的 Run 必须通过 SessionRuntime 恢复")
    runtime.bind_run(coordinator)
    differences = coordinator.definition_differences(
        provider=runtime.config.active,
        model=runtime.config.active_provider.model,
        system_prompt=runtime.loop.system_prompt,
        tool_schemas=runtime.loop.tool_schemas,
    )
    if differences and coordinator.state.phase != "terminal":
        request = _definition_change_request(coordinator, None, differences)
        try:
            decision = runtime.interaction.confirm_definition_change(request)
        except Exception:
            decision = None
        if decision is None or decision.request_id != request.request_id or not decision.accepted:
            return RunExecution(run_id, iter(()), _load_warning(coordinator))
        coordinator.accept_definitions(
            provider=runtime.config.active,
            model=runtime.config.active_provider.model,
            system_prompt=runtime.loop.system_prompt,
            tool_schemas=runtime.loop.tool_schemas,
        )
    coordinator.note_resume()
    return RunExecution(
        run_id,
        runtime.loop.resume(
            coordinator,
            recovery_check=lambda call: _recovery_choice(runtime, coordinator, call, None),
        ),
        _load_warning(coordinator),
    )


def sync_terminal_session(
    coordinator: RunCoordinator,
    store: SessionRepository,
    session: Session | None = None,
) -> Session | None:
    """把 terminal Run 幂等同步到 Session，保存成功后再提交 session_synced。"""
    state = coordinator.state
    if state.status not in {"cancelled", "completed", "failed"} or state.session_id is None:
        return session
    if state.session_synced:
        return session or store.load(state.session_id)
    if session is None:
        session = store.load(state.session_id)
    session.provider = state.provider
    session.model = state.model
    session.compaction_checkpoint = state.compaction_checkpoint
    by_id = {item.artifact_id: item for item in session.presentations}
    for artifact in state.presentations:
        existing = by_id.get(artifact.artifact_id)
        if existing is not None and existing.content_hash != artifact.content_hash:
            continue
        by_id.setdefault(artifact.artifact_id, artifact)
    session.presentations = list(by_id.values())
    _synchronize_run_ledger(session, state)
    message_id = stable_message_id(state.run_id)
    if not any(item.id == message_id for item in session.assistant_messages):
        assistant_content = next(
            (
                str(message.get("content") or "")
                for message in reversed(state.messages)
                if message.get("role") == "assistant" and not message.get("tool_calls")
            ),
            state.terminal_text if state.status == "completed" else "",
        )
        session.assistant_messages.append(
            AssistantMessageSnapshot(
                id=message_id,
                content=assistant_content,
                artifacts=tuple(item.ref for item in state.presentations),
            )
        )
    store.save(session, state.messages, must_exist=True)
    coordinator.mark_session_synced()
    return session


def _synchronize_run_ledger(session: Session, state: RunState) -> None:
    public = [
        message
        for message in state.messages
        if message.get("role") == "user"
        or (message.get("role") == "assistant" and not message.get("tool_calls"))
    ]
    ledger = session.message_ledger
    if len(public) < len(ledger):
        raise SessionMigrationRequiredError("Run 历史短于 Session ledger")
    for raw, saved in zip(public, ledger, strict=False):
        content_matches = (
            parse_message_content(raw.get("content")).model_dump(mode="json")
            == saved.content.model_dump(mode="json")
            if saved.role == "user" and not isinstance(saved.content, str)
            else str(raw.get("content") or "") == saved.content
        )
        if raw.get("role") != saved.role or not content_matches:
            raise SessionMigrationRequiredError("Run 历史与 Session ledger 冲突")
    current_user_id = next(
        (message.id for message in reversed(ledger) if message.role == "user"), None
    )
    appended = public[len(ledger) :]
    for offset, raw in enumerate(appended):
        role = cast(Literal["user", "assistant"], raw.get("role"))
        artifacts: tuple[PresentationArtifactRefV2, ...]
        if role == "user":
            message_id = _run_message_id(state.run_id, "user", offset)
            current_user_id = message_id
            reply_to = None
            created_at = state.created_at
            artifacts = ()
        else:
            if current_user_id is None:
                raise SessionMigrationRequiredError("assistant message 缺少对应 user")
            message_id = (
                stable_message_id(state.run_id)
                if offset == len(appended) - 1
                else _run_message_id(state.run_id, "assistant", offset)
            )
            reply_to = current_user_id
            created_at = state.updated_at
            artifacts = tuple(item.ref for item in state.presentations)
            if any(ref.message_id != message_id for ref in artifacts):
                raise SessionMigrationRequiredError("Run Artifact 与 assistant message 不一致")
        ledger.append(
            PublicMessageSnapshot(
                id=message_id,
                role=role,
                created_at=created_at,
                reply_to_message_id=reply_to,
                content=(
                    parse_message_content(raw.get("content"))
                    if role == "user"
                    else str(raw.get("content") or "")
                ),
                artifacts=artifacts,
            )
        )
    artifact_message_id = stable_message_id(state.run_id)
    if state.presentations and not any(item.id == artifact_message_id for item in ledger):
        if current_user_id is None:
            raise SessionMigrationRequiredError("Run Artifact 缺少对应 user")
        refs = tuple(item.ref for item in state.presentations)
        if any(ref.message_id != artifact_message_id for ref in refs):
            raise SessionMigrationRequiredError("Run Artifact message ID 不一致")
        ledger.append(
            PublicMessageSnapshot(
                id=artifact_message_id,
                role="assistant",
                created_at=state.updated_at,
                reply_to_message_id=current_user_id,
                content="",
                artifacts=refs,
            )
        )


def _run_message_id(run_id: str, role: str, ordinal: int) -> str:
    payload = f"run-message:{run_id}:{role}:{ordinal}".encode()
    return "msg_" + hashlib.sha256(payload).hexdigest()[:24]


def _session_snapshot(
    session: Session,
    *,
    presentations: tuple[ChartArtifactV2, ...] | None = None,
    fork_created: bool | None = None,
) -> SessionSnapshot:
    assistant_messages = tuple(
        AssistantMessageSnapshot(
            id=message.id,
            content=cast(str, message.content),
            artifacts=message.artifacts,
        )
        for message in session.message_ledger
        if message.role == "assistant"
    )
    return SessionSnapshot(
        id=session.id,
        title=session.title,
        title_source=cast(Literal["auto", "user"], session.title_source),
        metadata_version=session.metadata_version,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=tuple(session.message_ledger),
        artifacts=tuple(
            item.ref
            for item in (presentations if presentations is not None else session.presentations)
        ),
        assistant_messages=assistant_messages,
        fork_created=fork_created,
    )


def _load_warning(coordinator: RunCoordinator) -> str:
    return coordinator.load_info.warning if coordinator.load_info is not None else ""


def _definition_change_request(
    coordinator: RunCoordinator,
    session_id: str | None,
    differences: list[DefinitionDifference],
) -> DefinitionChangeRequest:
    return DefinitionChangeRequest(
        run_id=coordinator.run_id,
        session_id=session_id,
        differences=tuple(
            DefinitionDifferenceInfo(
                item.field,
                canonical_hash(item.saved),
                canonical_hash(item.current),
            )
            for item in differences
        ),
    )


def _recovery_choice(
    runtime: AgentRuntime,
    coordinator: RunCoordinator,
    call: ToolCallState,
    session_id: str | None,
) -> LoopRecoveryChoice:
    request = RecoveryRequest(
        run_id=coordinator.run_id,
        session_id=session_id,
        call_id=call.id,
        tool=call.name,
        display_summary=str(runtime.sanitize_for_display(call.arguments)),
        duplicate_side_effect_risk=(
            "上次执行结果未知；retry 可能重复产生文件、进程、网络或外部系统副作用。"
        ),
    )
    try:
        decision = runtime.interaction.decide_recovery(request)
    except Exception:
        return "abort"
    if decision.request_id != request.request_id:
        return "abort"
    return decision.choice


class SessionRuntime:
    """一个 Session 的隔离 Runtime；同一时刻至多执行一个 Run。

    这个对象是服务调用的主要工作单元：它把 Conversation、Session 文档、Run checkpoint 和
    execution lease 绑定在一起。所有公开 Run 操作都先校验 Session 归属，避免调用方误操作其他
    Session 的 run_id。
    """

    def __init__(self, runtime: AgentRuntime, session: Session) -> None:
        self.runtime = runtime
        self.session = session
        # Session 保存长期历史；Loop 内的 Conversation 是模型本次要看到的工作副本。
        # compaction checkpoint 必须一起载入，否则恢复后会重复摘要或改变上下文边界。
        self.runtime.loop.load_history(session.messages)
        self.runtime.loop.load_checkpoint(session.compaction_checkpoint)
        self.runtime.logger.bind_session(session.id)
        self._lock = threading.Lock()
        self._active_run_id: str | None = None
        self._execution_lease: SessionExecutionLease | None = None
        self._active_execution: RunExecution | None = None
        self._closed = False
        self.runtime.bind_execution_close(self._close_active_execution)

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active_run_id

    @property
    def capabilities(self) -> RuntimeCapabilities:
        capabilities = self.runtime.capabilities_snapshot()
        if capabilities is None:
            raise RuntimeClosedError("Runtime 能力快照不可用")
        return capabilities

    def unfinished_runs(self) -> list[RunMeta]:
        return [
            item
            for item in self.runtime.run_store.list()
            if item.session_id == self.session.id and item.status in {"running", "paused"}
        ]

    def list_presentations(self) -> tuple[ChartArtifactV2, ...]:
        merged = {item.artifact_id: item for item in self.session.presentations}
        for meta in self.runtime.run_store.list():
            if meta.session_id != self.session.id:
                continue
            try:
                state = RunCoordinator.load(self.runtime.run_store, meta.id).state
            except Exception:
                continue
            if state.session_id != self.session.id:
                continue
            for artifact in state.presentations:
                merged.setdefault(artifact.artifact_id, artifact)
        return tuple(merged.values())

    def get_artifact(self, artifact_id: str) -> ChartArtifactV2:
        artifact = next(
            (item for item in self.list_presentations() if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None:
            raise ArtifactNotFoundError("图表 Artifact 不存在")
        return artifact

    def ingest_attachments(
        self, uploads: list[AttachmentUploadV1] | tuple[AttachmentUploadV1, ...]
    ) -> tuple[AttachmentSummaryV1, ...]:
        with self._lock:
            if self._closed or self.runtime.closed:
                raise RuntimeClosedError("Session Runtime 已关闭")
        return self.runtime.attachment_store.ingest(self.session.id, uploads)

    def get_attachment(self, attachment_id: str) -> AttachmentPayloadV1:
        return self.runtime.attachment_store.get_by_id(self.session.id, attachment_id)

    def delete_unbound_attachments(self, attachment_ids: tuple[str, ...]) -> int:
        return self.runtime.attachment_store.delete_unbound(self.session.id, attachment_ids)

    def _attachment_refs_from_session(self) -> tuple[AttachmentRefV1, ...]:
        refs: list[AttachmentRefV1] = []
        for message in self.session.message_ledger:
            if message.role == "user":
                content = message.content
                if not isinstance(content, str):
                    refs.extend(content.attachment_refs())
        return tuple(refs)

    def snapshot(self) -> SessionSnapshot:
        presentations = self.list_presentations()
        self.session = self.runtime.session_store.load(self.session.id)
        return _session_snapshot(self.session, presentations=presentations)

    def fork_session(self, before_user_message_id: str, idempotency_key: str) -> SessionSnapshot:
        if not isinstance(before_user_message_id, str) or not _PUBLIC_MESSAGE_ID.fullmatch(
            before_user_message_id
        ):
            raise InvalidForkRequestError("before_user_message_id 不合法")
        if (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 200
            or any(not 0x21 <= ord(char) <= 0x7E for char in idempotency_key)
        ):
            raise InvalidIdempotencyKeyError("idempotency_key 必须是 1-200 个可见 ASCII 字符")
        with self._lock:
            if self._closed or self.runtime.closed:
                raise RuntimeClosedError("Session Runtime 已关闭")
        key_hash = canonical_hash(
            {"operation": "fork-key", "source_session_id": self.session.id, "key": idempotency_key}
        )
        request_hash = canonical_hash(
            {
                "operation": "fork-session",
                "source_session_id": self.session.id,
                "before_user_message_id": before_user_message_id,
            }
        )
        try:
            target, created = self.runtime.session_store.fork_session(
                self.session.id,
                before_user_message_id,
                key_hash,
                request_hash,
            )
            return _session_snapshot(target, fork_created=created)
        except (UserMessageNotFoundError, IdempotencyConflictError, SessionMigrationRequiredError):
            raise
        except FileNotFoundError as exc:
            raise SessionNotFoundError("源 Session 不存在") from exc
        except AgentServiceError:
            raise
        except (OSError, ValueError) as exc:
            raise SessionUnavailableError("Session fork 暂不可用") from exc

    def run_snapshot(self, run_id: str) -> RunSnapshot:
        coordinator = self._load_coordinator(run_id)
        if coordinator.state.session_id != self.session.id:
            raise ArtifactNotFoundError("Run 不属于当前 Session")
        state = coordinator.state
        terminal_status: TerminalStatus | None = (
            state.status if state.status in {"completed", "failed", "paused", "cancelled"} else None
        )
        return RunSnapshot(
            id=run_id,
            session_id=state.session_id,
            status=state.status,
            phase=state.phase,
            updated_at=state.updated_at,
            preview=state.task[:40] + ("…" if len(state.task) > 40 else ""),
            terminal_status=terminal_status,
            failure=state.failure,
            current_phase=state.phase,
            budget=BudgetSnapshot(
                iterations_used=state.iteration,
                iterations_limit=state.iteration_budget,
                tool_calls_used=state.tool_budget.used_calls,
                tool_calls_limit=state.tool_budget.max_calls,
                tool_output_chars_used=state.tool_budget.used_output_chars,
                tool_output_chars_limit=state.tool_budget.max_total_output_chars,
            ),
            pending_interaction=self._pending_interaction(run_id),
            final_candidate=state.terminal_text if state.status == "completed" else None,
            artifacts=tuple(item.ref for item in state.presentations),
            allowed_actions=self._allowed_actions(coordinator),
            execution_status=(
                "active"
                if self.active_run_id == run_id
                else ("unknown" if state.status == "running" else "inactive")
            ),
            retry_of_run_id=state.retry_of_run_id,
        )

    def start_run(self, task: str | UserMessageInputV1) -> RunExecution:
        user_input = UserMessageInputV1.from_text(task) if isinstance(task, str) else task
        task_text = user_input.content.safe_preview()
        refs = user_input.content.attachment_refs()
        if any(ref.session_id != self.session.id for ref in refs):
            raise AttachmentInvalidError("Attachment 不属于当前 Session")
        for ref in refs:
            self.runtime.attachment_store.get(ref)
        if (
            any(ref.kind == "image" for ref in refs)
            and "image" not in self.capabilities.input_modalities
        ):
            raise UnsupportedInputModalityError("当前模型不支持图片输入")
        used_tokens = attachment_token_estimate(
            user_input.content,
            image_reserve=self.runtime.config.active_provider.unknown_image_token_reserve,
        )
        available = max(
            self.runtime.config.agent.max_context_tokens
            - self.runtime.config.agent.reserved_output_tokens,
            0,
        )
        limit_tokens = min(
            self.runtime.config.attachments.max_context_tokens,
            int(available * self.runtime.config.attachments.max_context_ratio),
        )
        if used_tokens > limit_tokens:
            raise AttachmentContextTooLargeError(
                "附件上下文成本超过当前模型预算",
                used_tokens=used_tokens,
                limit_tokens=limit_tokens,
            )
        self._begin_run(None)
        self._acquire_execution_lease()
        self.runtime.run_control.reset()
        try:
            unfinished = self.unfinished_runs()
            if unfinished:
                raise SessionRunConflictError(
                    f"Session 存在未完成 Run：{', '.join(item.id for item in unfinished)}"
                )
            coordinator = self.runtime.new_run(task_text, self.session.id)
            if coordinator is None:
                raise SessionRunConflictError("公共服务运行要求启用 agent.recovery")
            self._set_active_id(coordinator.run_id)
            self.runtime.attachment_store.bind(
                self.session.id, tuple(ref.attachment_id for ref in refs)
            )
            self.runtime.logger.task(task_text)
            return self._owned_execution(
                coordinator,
                self._stream(
                    coordinator,
                    self.runtime.loop.run(user_input, coordinator=coordinator),
                ),
            )
        except BaseException:
            self._end_run()
            raise

    def pause(self) -> None:
        if self.active_run_id is not None:
            self.runtime.run_control.request_pause()
            self._interrupt_interaction()

    def cancel(self) -> None:
        if self.active_run_id is not None:
            self.runtime.run_control.request_cancel()
            self._interrupt_interaction()

    def cancel_run(self, run_id: str) -> RunExecution:
        """取消 worker 已退出的 paused Run，并返回唯一的新终态事件。"""
        self._begin_run(run_id)
        self._acquire_execution_lease()
        try:
            coordinator = self._load_coordinator(run_id, with_logger=True)
            state = coordinator.state
            if state.session_id != self.session.id:
                raise SessionRunConflictError("Run 不属于当前 Session")
            if state.status == "running":
                raise SessionBusyError("Run 仍由执行 worker 管理，请通过 cancel() 请求取消")
            if state.status in {"completed", "failed"}:
                raise SessionRunConflictError(f"已结束的 {state.status} Run 不能改写为 cancelled")
            if state.status == "cancelled":
                self._finish_run(coordinator)
                return RunExecution(run_id, iter(()), _load_warning(coordinator))

            coordinator.cancel(
                "任务已取消",
                messages=state.messages,
                compaction_checkpoint=state.compaction_checkpoint,
            )
            finalized = self._finish_run(coordinator)
            terminal = StepEvent(
                kind="run_terminal",
                text=coordinator.state.terminal_text,
                terminal_status="cancelled",
            )
            notices = tuple(self._finalization_notice(code) for code in finalized.notice_codes)
            return RunExecution(run_id, iter((*notices, terminal)), _load_warning(coordinator))
        finally:
            self._end_run()

    def _interrupt_interaction(self) -> None:
        interrupt = getattr(self.runtime.interaction, "interrupt_pending", None)
        if callable(interrupt):
            interrupt()

    def resume_run(self, run_id: str) -> RunExecution:
        self._begin_run(run_id)
        self._acquire_execution_lease()
        self.runtime.run_control.reset()
        try:
            coordinator = self._load_coordinator(run_id, with_logger=True)
            if coordinator.state.session_id != self.session.id:
                raise SessionRunConflictError("Run 不属于当前 Session")
            if coordinator.state.status != "paused":
                raise RunNotResumableError(
                    f"只有 paused Run 可以恢复，当前状态为 {coordinator.state.status}"
                )
            self._set_active_id(run_id)
            self.runtime.bind_run(coordinator)
            differences = coordinator.definition_differences(
                provider=self.runtime.config.active,
                model=self.runtime.config.active_provider.model,
                system_prompt=self.runtime.loop.system_prompt,
                tool_schemas=self.runtime.loop.tool_schemas,
            )
            if differences and coordinator.state.phase != "terminal":
                request = _definition_change_request(coordinator, self.session.id, differences)
                try:
                    decision = self.runtime.interaction.confirm_definition_change(request)
                except Exception:
                    decision = None
                if (
                    decision is None
                    or decision.request_id != request.request_id
                    or not decision.accepted
                ):
                    if self.runtime.run_control.state is ControlState.CANCEL_REQUESTED:
                        coordinator.cancel(
                            "任务已强制取消",
                            messages=self.runtime.loop.export_history(),
                            compaction_checkpoint=self.runtime.loop.export_checkpoint(),
                        )
                        return self._owned_execution(
                            coordinator,
                            self._stream(coordinator, iter(())),
                            _load_warning(coordinator),
                        )
                    coordinator.pause("Run 定义变化未获确认，保持暂停。")
                    return self._owned_execution(
                        coordinator,
                        self._paused_stream(coordinator),
                        _load_warning(coordinator),
                    )
                coordinator.accept_definitions(
                    provider=self.runtime.config.active,
                    model=self.runtime.config.active_provider.model,
                    system_prompt=self.runtime.loop.system_prompt,
                    tool_schemas=self.runtime.loop.tool_schemas,
                )
            coordinator.note_resume()
            return self._owned_execution(
                coordinator,
                self._stream(
                    coordinator,
                    self.runtime.loop.resume(
                        coordinator,
                        recovery_check=lambda call: _recovery_choice(
                            self.runtime, coordinator, call, self.session.id
                        ),
                    ),
                ),
                _load_warning(coordinator),
            )
        except BaseException:
            self._end_run()
            raise

    def reconcile_orphaned_run(self, run_id: str, idempotency_key: str) -> RunExecution:
        request_hash = self._idempotency_hash("reconcile", run_id, idempotency_key)
        self._begin_run(run_id)
        self._acquire_execution_lease()
        try:
            coordinator = self._load_coordinator(run_id, with_logger=True)
            state = coordinator.state
            if state.session_id != self.session.id:
                raise SessionRunConflictError("Run 不属于当前 Session")
            if state.status == "paused" and state.reconciliation_request_hash == request_hash:
                return RunExecution(run_id, iter(()), _load_warning(coordinator))
            if state.status != "running":
                raise RunNotReconcilableError(
                    f"只有遗留 running Run 可以协调，当前状态为 {state.status}"
                )
            coordinator.reconcile_orphan(request_hash)
            return RunExecution(
                run_id,
                iter(
                    (
                        StepEvent(
                            kind="run_terminal",
                            text=coordinator.state.terminal_text,
                            terminal_status="paused",
                            failure=coordinator.state.failure,
                        ),
                    )
                ),
                _load_warning(coordinator),
            )
        finally:
            self._end_run()

    def retry_failed_run(self, run_id: str, idempotency_key: str) -> RetryRunExecution:
        key_hash = self._idempotency_hash("key", "", idempotency_key)
        request_hash = self._idempotency_hash("retry", run_id, idempotency_key)
        self._begin_run(run_id)
        self._acquire_execution_lease()
        try:
            original = self._load_coordinator(run_id, with_logger=True)
            state = original.state
            if state.session_id != self.session.id:
                raise SessionRunConflictError("Run 不属于当前 Session")
            existing_id = state.retry_requests.get(key_hash)
            if existing_id is not None:
                target = self._retry_target(original, existing_id, key_hash, request_hash)
                if target is not None:
                    self._end_run()
                    return RetryRunExecution(
                        run_id, target.run_id, False, iter(()), _load_warning(original)
                    )
                original.remove_retry(key_hash, existing_id)
            self._ensure_retryable(original)
            unfinished = [item for item in self.unfinished_runs() if item.id != run_id]
            if unfinished:
                raise SessionBusyError(
                    f"Session 存在未完成 Run：{', '.join(item.id for item in unfinished)}"
                )
            for meta in self.runtime.run_store.list():
                if meta.session_id != self.session.id:
                    continue
                candidate = RunCoordinator.load(self.runtime.run_store, meta.id).state
                if candidate.retry_idempotency_key_hash != key_hash:
                    continue
                if (
                    candidate.session_id != self.session.id
                    or candidate.retry_of_run_id != run_id
                    or candidate.retry_request_hash != request_hash
                ):
                    raise IdempotencyConflictError("幂等键已用于其他重试请求")
                original.record_retry(key_hash, candidate.run_id)
                self._end_run()
                return RetryRunExecution(run_id, candidate.run_id, False, iter(()))

            self.runtime.loop.load_history(state.baseline_messages)
            self.runtime.loop.load_checkpoint(state.baseline_compaction_checkpoint)
            coordinator = self.runtime.new_run(state.task, self.session.id)
            if coordinator is None:
                raise SessionRunConflictError("公共服务运行要求启用 agent.recovery")
            coordinator.prepare_retry(
                retry_of_run_id=run_id,
                idempotency_key_hash=key_hash,
                request_hash=request_hash,
            )
            coordinator.checkpoint()
            original.record_retry(key_hash, coordinator.run_id)
            self._set_active_id(coordinator.run_id)
            self.runtime.logger.task(state.task)
            execution = self._owned_execution(
                coordinator,
                self._stream(
                    coordinator,
                    self.runtime.loop.run(state.task, coordinator=coordinator),
                ),
            )
            return RetryRunExecution(
                run_id,
                coordinator.run_id,
                True,
                execution.events,
            )
        except BaseException:
            self._end_run()
            raise

    @staticmethod
    def _ensure_retryable(coordinator: RunCoordinator) -> None:
        state = coordinator.state
        if state.status != "failed" or state.failure is None:
            raise RunNotRetryableError("只有 failed Run 可以重新运行")
        if not state.retry_baseline_available:
            raise RunNotRetryableError("Run 未保存可靠的会话基线，不能安全重试")
        failure = state.failure
        if failure.unknown_side_effect or state.retry_safety == "uncertain":
            raise RunRecoveryRequiredError("Run 存在结果未知的副作用，必须先恢复")
        if (
            not failure.retryable
            or "retry_run" not in failure.allowed_actions
            or state.retry_safety != "safe"
        ):
            raise RunNotRetryableError("Run 不满足安全重试条件")

    def _retry_target(
        self,
        original: RunCoordinator,
        target_run_id: str,
        key_hash: str,
        request_hash: str,
    ) -> RunState | None:
        try:
            target = RunCoordinator.load(self.runtime.run_store, target_run_id).state
        except FileNotFoundError:
            return None
        if (
            target.session_id != self.session.id
            or target.retry_of_run_id != original.run_id
            or target.retry_idempotency_key_hash != key_hash
            or target.retry_request_hash != request_hash
        ):
            raise IdempotencyConflictError("重试幂等记录指向无效 Run")
        return target

    @staticmethod
    def _idempotency_hash(operation: str, run_id: str, key: str) -> str:
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("idempotency_key 必须是 1-200 字符的非空字符串")
        return canonical_hash({"operation": operation, "run_id": run_id, "key": key})

    def _allowed_actions(self, coordinator: RunCoordinator) -> tuple[AllowedAction, ...]:
        state = coordinator.state
        if state.status == "running":
            return ("stop",) if self.active_run_id == state.run_id else ("reconcile_run",)
        if state.status == "paused":
            if state.phase == "tool_uncertain":
                return ("resolve_uncertain_tool", "stop")
            return ("resume_run", "stop")
        if state.status == "failed" and state.failure is not None:
            if state.retry_safety == "safe":
                return state.failure.allowed_actions
            actions = tuple(
                action for action in state.failure.allowed_actions if action != "retry_run"
            )
            if state.retry_safety == "uncertain" and "resolve_uncertain_tool" not in actions:
                actions = (*actions, "resolve_uncertain_tool")
            return actions or ("start_new_run",)
        return ("start_new_run",)

    def _load_coordinator(self, run_id: str, *, with_logger: bool = False) -> RunCoordinator:
        try:
            return RunCoordinator.load(
                self.runtime.run_store,
                run_id,
                logger=self.runtime.logger if with_logger else None,
            )
        except FileNotFoundError as exc:
            raise RunNotFoundError("Run 不存在") from exc

    def _pending_interaction(self, run_id: str) -> PendingInteractionSnapshot | None:
        pending_requests = getattr(self.runtime.interaction, "pending_requests", None)
        if not callable(pending_requests):
            return None
        for request in pending_requests():
            if request.run_id == run_id:
                return PendingInteractionSnapshot(
                    request_id=request.request_id,
                    kind=request.kind,
                    expires_at=request.expires_at,
                    call_id=request.call_id,
                )
        return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.runtime.close("session_runtime_closed")

    def __enter__(self) -> SessionRuntime:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _begin_run(self, requested_run_id: str | None) -> None:
        with self._lock:
            if self._closed or self.runtime.closed:
                raise RuntimeClosedError("Session Runtime 已关闭")
            if self._active_run_id is not None:
                raise SessionBusyError(f"Session 已有活跃 Run：{self._active_run_id}")
            self._active_run_id = requested_run_id or "__creating__"

    def _set_active_id(self, run_id: str) -> None:
        with self._lock:
            self._active_run_id = run_id

    def _end_run(self) -> None:
        self.runtime.tool_context.clear_run()
        lease = self._execution_lease
        self._execution_lease = None
        if lease is not None:
            lease.release()
        with self._lock:
            self._active_run_id = None
            self._active_execution = None

    def _owned_execution(
        self,
        coordinator: RunCoordinator,
        source: Iterator[StepEvent],
        warning: str = "",
    ) -> RunExecution:
        events = _ExecutionEvents(
            source,
            lambda started: self._execution_finished(coordinator, started),
            self.runtime.run_control.request_cancel,
        )
        execution = RunExecution(coordinator.run_id, events, warning)
        with self._lock:
            self._active_execution = execution
        return execution

    def _execution_finished(self, coordinator: RunCoordinator, started: bool) -> None:
        try:
            if not started and coordinator.state.status == "running":
                coordinator.cancel(
                    "任务在执行开始前关闭。",
                    messages=coordinator.state.baseline_messages,
                    compaction_checkpoint=coordinator.state.baseline_compaction_checkpoint,
                )
                self._finish_run(coordinator)
        finally:
            self._end_run()

    def _close_active_execution(self) -> None:
        with self._lock:
            execution = self._active_execution
        if execution is not None:
            execution.close()

    def _acquire_execution_lease(self) -> None:
        try:
            self._execution_lease = self.runtime.execution_leases.acquire(self.session.id)
            self.runtime.session_store.load(self.session.id)
        except BaseException:
            self._end_run()
            raise

    def _paused_stream(self, coordinator: RunCoordinator) -> Iterator[StepEvent]:
        try:
            yield StepEvent(
                kind="run_terminal",
                text=coordinator.state.terminal_text,
                terminal_status="paused",
                failure=coordinator.state.failure,
            )
        finally:
            self._end_run()

    def _stream(
        self, coordinator: RunCoordinator, source: Iterator[StepEvent]
    ) -> Iterator[StepEvent]:
        exhausted = False
        try:
            yield from source
            exhausted = True
        except Exception:
            if coordinator.state.status == "running":
                coordinator.mark_uncertain_if_needed()
                if coordinator.state.status == "running":
                    coordinator.terminal(
                        success=False,
                        text="Agent 运行异常终止。",
                        messages=self.runtime.loop.export_history(),
                        compaction_checkpoint=self.runtime.loop.export_checkpoint(),
                        failure=RunFailure(
                            code="internal_error",
                            safe_message="Agent 运行异常终止。",
                            retryable=True,
                            allowed_actions=("retry_run", "start_new_run", "stop"),
                            terminal_status="failed",
                            phase="saving_checkpoint",
                        ),
                    )
            exhausted = True
        finally:
            try:
                if not exhausted and coordinator.state.status == "running":
                    self.runtime.run_control.request_pause()
                    coordinator.mark_uncertain_if_needed()
                    if coordinator.state.status == "running":
                        coordinator.pause(
                            "事件消费者提前关闭，Run 已安全暂停。",
                            messages=self.runtime.loop.export_history(),
                        )
                # 不变量：消费者提前关闭生成器时 exhausted 恒为 False（关闭发生在
                # source 耗尽之前），故此处的 yield 只在 source 正常走完后触发，不会撞上
                # GeneratorExit。改动上面的控制流时须维持这一点，否则 finally 内 yield 会抛错。
                if exhausted:
                    yield StepEvent(kind="activity", phase="syncing_session")
                    finalized = self._finish_run(coordinator)
                    for code in finalized.notice_codes:
                        yield self._finalization_notice(code)
                    yield StepEvent(
                        kind="run_terminal",
                        text=coordinator.state.terminal_text,
                        terminal_status=finalized.status,
                        failure=coordinator.state.failure,
                    )
            finally:
                self._end_run()

    @staticmethod
    def _finalization_notice(code: str) -> StepEvent:
        messages = {
            "session_sync_deferred": "Run 终态已保存，会话同步将在后续重试。",
            "run_prune_deferred": "Run 终态已保存，历史清理将在后续重试。",
        }
        return StepEvent(kind="notice", text=messages[code], result_code=code)

    def _finish_run(self, coordinator: RunCoordinator) -> _FinalizationResult:
        status = coordinator.state.status
        if status == "running":
            coordinator.pause(
                "Run 未产生终态，已安全暂停。",
                messages=self.runtime.loop.export_history(),
            )
            status = "paused"
        if status in {"completed", "failed", "cancelled"}:
            notices: list[str] = []
            try:
                synced = sync_terminal_session(
                    coordinator, self.runtime.session_store, self.session
                )
                if synced is not None:
                    self.session = synced
            except Exception:  # noqa: BLE001
                notices.append("session_sync_deferred")
            try:
                self.runtime.run_store.prune(self.runtime.config.agent.recovery.max_completed_runs)
            except Exception:  # noqa: BLE001
                notices.append("run_prune_deferred")
            return _FinalizationResult(status, tuple(notices))
        return _FinalizationResult(status)
