"""Session CRUD 与隔离 Runtime 创建用例。"""

from __future__ import annotations

from assistant_agent.application.models import RunMeta, RunResumeInfo, SessionMeta
from assistant_agent.application.ports import (
    RunCatalogRepository,
    RuntimeFactoryPort,
    SessionRepository,
)
from assistant_agent.application.runs import SessionRuntime, inspect_run
from assistant_agent.contracts.capabilities import RuntimeCapabilities
from assistant_agent.contracts.errors import (
    RuntimeClosedError,
    SessionRunConflictError,
)
from assistant_agent.contracts.interactions import InteractionPort


class AgentService:
    """只依赖 RuntimeFactory 和 repository ports 的 Session 用例。"""

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactoryPort,
        session_store: SessionRepository,
        run_store: RunCatalogRepository,
        max_completed_runs: int,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._session_store = session_store
        self._run_store = run_store
        self._max_completed_runs = max_completed_runs

    def create_session(
        self,
        *,
        interaction: InteractionPort | None = None,
        interactive: bool = True,
    ) -> SessionRuntime:
        runtime = self._runtime_factory(interaction, interactive, None)
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
        runtime = self._runtime_factory(interaction, interactive, session_id)
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

    def inspect_run(self, run_id: str) -> RunResumeInfo:
        return inspect_run(self._run_store, run_id)

    def delete_run(self, run_id: str, *, force: bool = False) -> bool:
        meta = next((item for item in self._run_store.list() if item.id == run_id), None)
        if meta is None:
            return False
        if meta.status in {"running", "paused"} and not force:
            raise SessionRunConflictError(f"Run 尚未结束：{run_id}")
        return self._run_store.delete(run_id)

    def prune_completed_runs(self) -> list[str]:
        return self._run_store.prune(self._max_completed_runs)

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
        runtime = self._runtime_factory(None, False, None)
        try:
            if runtime.capabilities is None:
                raise RuntimeClosedError("Runtime 能力快照不可用")
            return runtime.capabilities
        finally:
            runtime.close("capability_probe_completed")
