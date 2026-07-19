"""Session/Run 的公共同步服务门面。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass

from assistant_agent.agent.run.coordinator import RecoveryChoice as LoopRecoveryChoice
from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.agent.run.ports import ControlState
from assistant_agent.agent.run.recovery import DefinitionDifference
from assistant_agent.agent.run.state import ToolCallState, canonical_hash
from assistant_agent.application.models import RunMeta, RunResumeInfo, Session
from assistant_agent.application.ports import RunCatalogRepository, SessionRepository
from assistant_agent.application.runtime import AgentRuntime
from assistant_agent.contracts.capabilities import RuntimeCapabilities
from assistant_agent.contracts.charts import (
    AssistantMessageSnapshot,
    ChartArtifact,
    RunSnapshot,
    SessionSnapshot,
    stable_message_id,
)
from assistant_agent.contracts.errors import (
    ArtifactNotFoundError,
    RuntimeClosedError,
    SessionBusyError,
    SessionRunConflictError,
)
from assistant_agent.contracts.events import StepEvent, TerminalStatus
from assistant_agent.contracts.interactions import (
    DefinitionChangeRequest,
    DefinitionDifferenceInfo,
    RecoveryRequest,
)


@dataclass(frozen=True)
class RunExecution:
    run_id: str
    events: Iterator[StepEvent]
    warning: str = ""


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
        try:
            session = store.load(state.session_id)
        except FileNotFoundError:
            session = Session(
                id=state.session_id,
                created_at=state.created_at,
                updated_at=state.updated_at,
            )
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
    store.save(session, state.messages)
    coordinator.mark_session_synced()
    return session


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
    """一个 Session 的隔离 Runtime；同一时刻至多执行一个 Run。"""

    def __init__(self, runtime: AgentRuntime, session: Session) -> None:
        self.runtime = runtime
        self.session = session
        self.runtime.loop.load_history(session.messages)
        self.runtime.loop.load_checkpoint(session.compaction_checkpoint)
        self.runtime.logger.bind_session(session.id)
        self._lock = threading.Lock()
        self._active_run_id: str | None = None
        self._closed = False

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

    def list_presentations(self) -> tuple[ChartArtifact, ...]:
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

    def get_artifact(self, artifact_id: str) -> ChartArtifact:
        artifact = next(
            (item for item in self.list_presentations() if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None:
            raise ArtifactNotFoundError("图表 Artifact 不存在")
        return artifact

    def snapshot(self) -> SessionSnapshot:
        presentations = self.list_presentations()
        return SessionSnapshot(
            id=self.session.id,
            assistant_messages=tuple(self.session.assistant_messages),
            artifacts=tuple(item.ref for item in presentations),
        )

    def run_snapshot(self, run_id: str) -> RunSnapshot:
        coordinator = RunCoordinator.load(self.runtime.run_store, run_id)
        if coordinator.state.session_id != self.session.id:
            raise ArtifactNotFoundError("Run 不属于当前 Session")
        return RunSnapshot(
            id=run_id,
            status=coordinator.state.status,
            artifacts=tuple(item.ref for item in coordinator.state.presentations),
        )

    def start_run(self, task: str) -> RunExecution:
        self._begin_run(None)
        self.runtime.run_control.reset()
        try:
            unfinished = self.unfinished_runs()
            if unfinished:
                raise SessionRunConflictError(
                    f"Session 存在未完成 Run：{', '.join(item.id for item in unfinished)}"
                )
            coordinator = self.runtime.new_run(task, self.session.id)
            if coordinator is None:
                raise SessionRunConflictError("公共服务运行要求启用 agent.recovery")
            self._set_active_id(coordinator.run_id)
            self.runtime.logger.task(task)
            return RunExecution(
                coordinator.run_id,
                self._stream(coordinator, self.runtime.loop.run(task, coordinator=coordinator)),
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

    def _interrupt_interaction(self) -> None:
        interrupt = getattr(self.runtime.interaction, "interrupt_pending", None)
        if callable(interrupt):
            interrupt()

    def resume_run(self, run_id: str) -> RunExecution:
        self._begin_run(run_id)
        self.runtime.run_control.reset()
        try:
            coordinator = RunCoordinator.load(
                self.runtime.run_store, run_id, logger=self.runtime.logger
            )
            if coordinator.state.session_id != self.session.id:
                raise SessionRunConflictError("Run 不属于当前 Session")
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
                        return RunExecution(
                            run_id,
                            self._stream(coordinator, iter(())),
                            _load_warning(coordinator),
                        )
                    coordinator.pause("Run 定义变化未获确认，保持暂停。")
                    return RunExecution(
                        run_id,
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
            return RunExecution(
                run_id,
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
        with self._lock:
            self._active_run_id = None

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
                    status = self._finish_run(coordinator)
                    yield StepEvent(
                        kind="run_terminal",
                        text=coordinator.state.terminal_text,
                        terminal_status=status,
                        failure=coordinator.state.failure,
                    )
            finally:
                self._end_run()

    def _finish_run(self, coordinator: RunCoordinator) -> TerminalStatus:
        status = coordinator.state.status
        if status == "running":
            coordinator.pause(
                "Run 未产生终态，已安全暂停。",
                messages=self.runtime.loop.export_history(),
            )
            status = "paused"
        if status in {"completed", "failed", "cancelled"}:
            synced = sync_terminal_session(coordinator, self.runtime.session_store, self.session)
            if synced is not None:
                self.session = synced
            self.runtime.run_store.prune(self.runtime.config.agent.recovery.max_completed_runs)
        return status
