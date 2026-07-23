"""为 Application AgentService 注入默认本地 adapter。

``application.sessions.AgentService`` 只依赖 ports，便于测试和替换存储；本模块是很薄的
composition adapter，负责选择文件 Store 和公共 Runtime 工厂。业务用例不得搬回这里。
"""

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
from assistant_agent.persistence.attachments import AttachmentStore
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
    """面向调用方的便捷构造器，仅负责默认 composition。

    继承不是为了覆盖用例，而是给 Application Service 注入本地文件实现。真正的 Session/Run
    行为仍由父类拥有，避免 CLI、API 各形成一套状态机。
    """

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
        attachment_store = AttachmentStore(paths.attachments, config.attachments)
        session_store = SessionStore(
            paths.sessions,
            lifecycle_dir=lifecycle_dir,
            attachment_store=attachment_store,
        )
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
            attachment_store=attachment_store,
        )
