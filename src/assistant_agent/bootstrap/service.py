"""为 Application AgentService 注入默认本地 adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from assistant_agent.application.capabilities import RuntimePolicy
from assistant_agent.application.runtime import AgentRuntime
from assistant_agent.application.sessions import AgentService as ApplicationAgentService
from assistant_agent.bootstrap.runtime import create_runtime
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.paths import resolve_run_dir, state_paths
from assistant_agent.contracts.errors import RuntimeConfigError
from assistant_agent.contracts.interactions import InteractionPort
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore


@dataclass(frozen=True)
class _DefaultRuntimeFactory:
    config_path: Path
    workspace_root: Path
    runtime_policy: RuntimePolicy

    def __call__(
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


class AgentService(ApplicationAgentService):
    """保持既有构造签名，仅负责默认 composition。"""

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
        paths = state_paths(self.workspace_root)
        lifecycle_dir = paths.workspace / "session-lifecycle"
        session_store = SessionStore(paths.sessions, lifecycle_dir=lifecycle_dir)
        run_store = RunStore(
            resolve_run_dir(config.agent.recovery.dir, self.workspace_root),
            lifecycle_dir=lifecycle_dir,
        )
        super().__init__(
            runtime_factory=_DefaultRuntimeFactory(
                self.config_path,
                self.workspace_root,
                self.runtime_policy,
            ),
            session_store=session_store,
            run_store=run_store,
            session_leases=FileSessionExecutionLeaseManager(paths.workspace / "execution-leases"),
            max_completed_runs=config.agent.recovery.max_completed_runs,
        )
