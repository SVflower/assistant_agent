"""Session/Run 的公共同步服务门面。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from assistant_agent.agent.recovery import RecoveryChoice as LoopRecoveryChoice
from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.agent.run_state import ToolCallState, canonical_hash
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.paths import resolve_run_dir, state_paths
from assistant_agent.interaction import (
    DefinitionChangeRequest,
    DefinitionDifferenceInfo,
    InteractionPort,
    RecoveryRequest,
)
from assistant_agent.obs import sanitize_for_display
from assistant_agent.service.capabilities import RuntimeCapabilities
from assistant_agent.service.errors import (
    RuntimeClosedError,
    RuntimeConfigError,
    SessionBusyError,
    SessionRunConflictError,
)
from assistant_agent.service.events import StepEvent, TerminalStatus
from assistant_agent.service.policy import RuntimePolicy
from assistant_agent.service.runtime import AgentRuntime, create_runtime
from assistant_agent.session.run_store import RunMeta, RunStore
from assistant_agent.session.store import Session, SessionMeta, SessionStore


@dataclass(frozen=True)
class RunExecution:
    run_id: str
    events: Iterator[StepEvent]


def sync_terminal_session(
    coordinator: RunCoordinator,
    store: SessionStore,
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
    store.save(session, state.messages)
    coordinator.mark_session_synced()
    return session


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
        if self.runtime.capabilities is None:
            raise RuntimeClosedError("Runtime 能力快照不可用")
        return self.runtime.capabilities

    def unfinished_runs(self) -> list[RunMeta]:
        return [
            item
            for item in self.runtime.run_store.list()
            if item.session_id == self.session.id and item.status in {"running", "paused"}
        ]

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

    def cancel(self) -> None:
        if self.active_run_id is not None:
            self.runtime.run_control.request_cancel()

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
                request = DefinitionChangeRequest(
                    run_id=run_id,
                    session_id=self.session.id,
                    differences=tuple(
                        DefinitionDifferenceInfo(
                            item.field,
                            canonical_hash(item.saved),
                            canonical_hash(item.current),
                        )
                        for item in differences
                    ),
                )
                try:
                    decision = self.runtime.interaction.confirm_definition_change(request)
                except Exception:
                    decision = None
                if (
                    decision is None
                    or decision.request_id != request.request_id
                    or not decision.accepted
                ):
                    coordinator.pause("Run 定义变化未获确认，保持暂停。")
                    return RunExecution(run_id, self._paused_stream(coordinator))
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
                        recovery_check=lambda call: self._recovery_choice(coordinator, call),
                    ),
                ),
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
                    status = self._finish_run(coordinator)
                    yield StepEvent(
                        kind="run_terminal",
                        text=coordinator.state.terminal_text,
                        terminal_status=status,
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

    def _recovery_choice(
        self, coordinator: RunCoordinator, call: ToolCallState
    ) -> LoopRecoveryChoice:
        request = RecoveryRequest(
            run_id=coordinator.run_id,
            session_id=self.session.id,
            call_id=call.id,
            tool=call.name,
            display_summary=str(sanitize_for_display(call.arguments)),
            duplicate_side_effect_risk=(
                "上次执行结果未知；retry 可能重复产生文件、进程、网络或外部系统副作用。"
            ),
        )
        try:
            decision = self.runtime.interaction.decide_recovery(request)
        except Exception:
            return "abort"
        if decision.request_id != request.request_id:
            return "abort"
        return decision.choice


class AgentService:
    """创建隔离 Session Runtime 并提供 Session CRUD 的稳定入口。"""

    def __init__(
        self,
        *,
        config_path: Path,
        workspace_root: Path,
        runtime_policy: RuntimePolicy | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.runtime_policy = runtime_policy or RuntimePolicy.cli()
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            raise RuntimeConfigError(str(exc)) from exc
        self._config = config
        paths = state_paths(self.workspace_root)
        self._session_store = SessionStore(paths.sessions)
        self._run_store = RunStore(resolve_run_dir(config.agent.recovery.dir, self.workspace_root))

    def create_session(
        self,
        *,
        interaction: InteractionPort | None = None,
        interactive: bool = True,
    ) -> SessionRuntime:
        runtime = self._create_runtime(interaction, interactive, None)
        try:
            session = runtime.session_store.new_session(
                provider=runtime.config.active,
                model=runtime.config.active_provider.model,
            )
            runtime.session_store.save(session, [])
            runtime.logger.bind_session(session.id)
            return SessionRuntime(runtime, session)
        except BaseException:
            runtime.close("session_create_failed")
            raise

    def load_session(
        self,
        session_id: str,
        *,
        interaction: InteractionPort | None = None,
        interactive: bool = True,
    ) -> SessionRuntime:
        runtime = self._create_runtime(interaction, interactive, session_id)
        try:
            session = runtime.session_store.load(session_id)
            return SessionRuntime(runtime, session)
        except BaseException:
            runtime.close("session_load_failed")
            raise

    def list_sessions(self) -> list[SessionMeta]:
        return self._session_store.list()

    def list_runs(self, *, session_id: str | None = None) -> list[RunMeta]:
        runs = self._run_store.list()
        return (
            runs if session_id is None else [item for item in runs if item.session_id == session_id]
        )

    def prune_completed_runs(self) -> list[str]:
        return self._run_store.prune(self._config.agent.recovery.max_completed_runs)

    def delete_session(self, session_id: str, *, force: bool = False) -> bool:
        unfinished = [
            item
            for item in self._run_store.list()
            if item.session_id == session_id and item.status in {"running", "paused"}
        ]
        if unfinished and not force:
            raise SessionRunConflictError(
                f"Session 存在未完成 Run：{', '.join(item.id for item in unfinished)}"
            )
        return self._session_store.delete(session_id)

    def probe_capabilities(self) -> RuntimeCapabilities:
        """创建一次隔离 Runtime 获取能力快照，并始终清理全部资源。"""
        runtime = self._create_runtime(None, False, None)
        try:
            if runtime.capabilities is None:
                raise RuntimeClosedError("Runtime 能力快照不可用")
            return runtime.capabilities
        finally:
            runtime.close("capability_probe_completed")

    def _create_runtime(
        self,
        interaction: InteractionPort | None,
        interactive: bool,
        session_id: str | None,
    ) -> AgentRuntime:
        return create_runtime(
            config_path=self.config_path,
            workspace_root=self.workspace_root,
            interaction=interaction,
            interactive=interactive,
            session_id=session_id,
            runtime_policy=self.runtime_policy,
        )
