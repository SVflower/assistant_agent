"""完整 Agent Runtime 的生命周期与 Run 创建。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.agent.run.ports import RunControlPort
from assistant_agent.application.ports import (
    Closable,
    MCPRuntimePort,
    RunCatalogRepository,
    RuntimeTelemetry,
    SessionExecutionLeaseManager,
    SessionRepository,
    SkillMetaPort,
)
from assistant_agent.config.schema import AppConfig
from assistant_agent.contracts.capabilities import RuntimeCapabilities, RuntimeNotice
from assistant_agent.contracts.errors import RuntimeClosedError
from assistant_agent.contracts.interactions import InteractionPort
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.ports import (
    ManagedProcessRegistryPort,
    ProcessSupervisorPort,
    WorkspacePort,
)


@dataclass
class AgentRuntime:
    config: AppConfig
    loop: AgentLoop
    logger: RuntimeTelemetry
    skill_store: Any
    tool_context: ToolContext
    interaction: InteractionPort
    session_store: SessionRepository
    run_store: RunCatalogRepository
    run_control: RunControlPort
    execution_leases: SessionExecutionLeaseManager
    process_supervisor: ProcessSupervisorPort
    process_manager: ManagedProcessRegistryPort | None = None
    sanitize_for_display: Callable[[Any], object] = lambda _value: "[hidden]"
    visible_skills: Sequence[SkillMetaPort] = ()
    notices: list[RuntimeNotice] = field(default_factory=list)
    skill_manager: Any = None
    mcp_service: Any = None
    web: Closable | None = None
    mcp: MCPRuntimePort | None = None
    interactive: bool = False
    workspace: WorkspacePort | None = None
    capabilities: RuntimeCapabilities | None = None
    _closed: bool = field(default=False, init=False)
    _close_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def skills_meta(self) -> list[tuple[str, str]]:
        return [(item.name, f"[{item.source}] {item.description}") for item in self.visible_skills]

    def capabilities_snapshot(self) -> RuntimeCapabilities | None:
        current = self.capabilities
        if current is None or self.mcp is None:
            return current
        return RuntimeCapabilities(
            sandbox=current.sandbox,
            tools=current.tools,
            skills=current.skills,
            mcp_servers=tuple(self.mcp.server_capabilities()),
            extension_management=current.extension_management,
            profile=current.profile,
        )

    def new_run(self, task: str, session_id: str | None = None) -> RunCoordinator | None:
        if self.closed:
            raise RuntimeClosedError("Runtime 已关闭")
        if not self.config.agent.recovery.enabled:
            return None
        coordinator = RunCoordinator.create(
            self.run_store,
            task=task,
            provider=self.config.active,
            model=self.config.active_provider.model,
            system_prompt=self.loop.system_prompt,
            tool_schemas=self.loop.tool_schemas,
            interactive=self.interactive,
            max_iterations=self.config.agent.max_iterations,
            max_tool_calls=self.config.agent.max_tool_calls,
            max_total_tool_output_chars=self.config.agent.max_total_tool_output_chars,
            continuation_max_extensions=self.config.agent.continuation.max_extensions,
            iteration_increment=self.config.agent.continuation.iteration_increment,
            max_iterations_hard=self.config.agent.continuation.max_iterations_hard,
            tool_call_increment=self.config.agent.continuation.tool_call_increment,
            max_tool_calls_hard=self.config.agent.continuation.max_tool_calls_hard,
            tool_output_increment=self.config.agent.continuation.tool_output_increment,
            max_tool_output_chars_hard=self.config.agent.continuation.max_tool_output_chars_hard,
            session_id=session_id,
            logger=self.logger,
        )
        self.tool_context.bind_run(coordinator.run_id, session_id)
        return coordinator

    def bind_run(self, coordinator: RunCoordinator) -> None:
        self.tool_context.bind_run(coordinator.run_id, coordinator.state.session_id)

    def __enter__(self) -> AgentRuntime:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self, reason: str = "") -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.run_control.request_cancel()
        resources: list[Callable[[], None]] = [self.interaction.close]
        if self.process_manager is not None:
            resources.append(self.process_manager.close)
        if self.mcp is not None:
            resources.append(self.mcp.close)
        if self.web is not None:
            resources.append(self.web.close)
        resources.append(
            self.workspace.close if self.workspace is not None else self.process_supervisor.close
        )
        for close in resources:
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                self.notices.append(RuntimeNotice("runtime_close_failed", str(exc)))
        self.tool_context.clear_run()
        self.logger.session_end(reason=reason)
