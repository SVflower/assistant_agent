"""UI 无关的 Agent Runtime 工厂与资源生命周期。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.paths import resolve_log_dir, resolve_run_dir, state_paths
from assistant_agent.config.schema import AppConfig
from assistant_agent.interaction import (
    ContinueRequest,
    InteractionPort,
    SafeDefaultInteractionPort,
)
from assistant_agent.llm.client import LLMClient
from assistant_agent.mcp import MCPManager, MCPService
from assistant_agent.obs import NullLogger, create_logger, new_trace_id
from assistant_agent.runtime import BaseWorkspace, ProcessSupervisor, RunControl
from assistant_agent.service._runtime_builders import (
    RuntimeNotice,
    build_permission_policy,
    discover_skills,
    register_extension_tools,
    start_mcp,
    start_web,
    start_workspace,
)
from assistant_agent.service.errors import (
    RuntimeClosedError,
    RuntimeConfigError,
    RuntimeInitializationError,
)
from assistant_agent.session.run_store import RunStore
from assistant_agent.session.store import SessionStore
from assistant_agent.skills import LoadSkillTool, SkillManager, SkillMeta, SkillStore
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.extensions import ConfigureMCPServerTool, ManageSkillTool
from assistant_agent.tools.registry import build_default_registry
from assistant_agent.web import WebClient


@dataclass
class AgentRuntime:
    config: AppConfig
    loop: AgentLoop
    logger: NullLogger
    skill_store: SkillStore
    tool_context: ToolContext
    interaction: InteractionPort
    session_store: SessionStore
    visible_skills: list[SkillMeta] = field(default_factory=list)
    notices: list[RuntimeNotice] = field(default_factory=list)
    skill_manager: SkillManager = field(default_factory=SkillManager)
    mcp_service: MCPService | None = None
    web: WebClient | None = None
    mcp: MCPManager | None = None
    run_store: RunStore = field(default_factory=RunStore)
    interactive: bool = False
    run_control: RunControl = field(default_factory=RunControl)
    process_supervisor: ProcessSupervisor = field(default_factory=ProcessSupervisor)
    workspace: BaseWorkspace | None = None
    _closed: bool = field(default=False, init=False)
    _close_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def skills_meta(self) -> list[tuple[str, str]]:
        return [(item.name, f"[{item.source}] {item.description}") for item in self.visible_skills]

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
            except Exception as exc:  # noqa: BLE001 - 关闭一个资源失败不能阻断其余清理
                self.notices.append(RuntimeNotice("runtime_close_failed", str(exc)))
        self.tool_context.clear_run()
        self.logger.session_end(reason=reason)


def create_runtime(
    *,
    config_path: Path,
    workspace_root: Path,
    interaction: InteractionPort | None = None,
    interactive: bool,
    session_id: str | None = None,
    interrupt_check: Callable[[], bool] | None = None,
    run_control: RunControl | None = None,
    provider: str | None = None,
    max_iterations: int | None = None,
) -> AgentRuntime:
    """创建 UI 无关 Runtime；失败抛类型化异常并回滚所有已创建资源。"""
    port = interaction or SafeDefaultInteractionPort()
    config_file = Path(config_path).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve()
    try:
        config = load_config(config_file)
    except ConfigError as exc:
        port.close()
        raise RuntimeConfigError(str(exc)) from exc
    if provider is not None:
        if provider not in config.providers:
            available = ", ".join(sorted(config.providers))
            port.close()
            raise RuntimeConfigError(f"未知 provider：{provider}。可选：{available}")
        config.active = provider
    if max_iterations is not None:
        if max_iterations < 1:
            port.close()
            raise RuntimeConfigError("max_iterations 必须大于 0")
        config.agent.max_iterations = max_iterations

    control = run_control or RunControl()
    supervisor = ProcessSupervisor()
    registry = build_default_registry()
    paths = state_paths(root)
    logging_config = config.logging.model_copy(
        update={"dir": str(resolve_log_dir(config.logging.dir, root))}
    )
    logger = create_logger(logging_config, new_trace_id(), session_id=session_id)
    workspace: BaseWorkspace | None = None
    web: WebClient | None = None
    mcp: MCPManager | None = None
    notices: list[RuntimeNotice] = []
    stage = "workspace"
    try:
        workspace, workspace_notices = start_workspace(config, root, control, supervisor)
        notices.extend(workspace_notices)
        logger.session_start(
            provider=config.active,
            model=config.active_provider.model,
            mode="chat" if interactive else "run",
            cwd=str(root),
        )
        if not config.tools.confirm_dangerous_shell:
            notices.append(
                RuntimeNotice(
                    "deprecated_confirm_dangerous_shell",
                    "tools.confirm_dangerous_shell=false 已废弃；权限仍由统一策略控制。",
                )
            )

        stage = "skills"
        skill_store = discover_skills(config.skills, root)
        skills = skill_store.list()
        visible_skills = sorted(
            (item for item in skills if item.trusted), key=lambda item: item.name
        )
        skipped = [f"{item.source}/{item.name}" for item in skills if not item.trusted]
        if skipped:
            notices.append(
                RuntimeNotice(
                    "skills_skipped_untrusted",
                    "未注入未显式信任的项目或自定义 Skill。",
                    details={"skills": skipped},
                )
            )
        if skills:
            registry.register(LoadSkillTool(skill_store))
        skill_meta = [(item.name, f"[{item.source}] {item.description}") for item in visible_skills]
        system_prompt = build_system_prompt(interactive, skills=skill_meta or None)

        tool_context = ToolContext(
            confirm_dangerous_shell=config.tools.confirm_dangerous_shell,
            shell_timeout=config.tools.shell_timeout,
            interaction=port,
            logger=logger,
            max_output_chars=config.tools.max_output_chars,
            max_captured_output_chars=config.tools.max_captured_output_chars,
            max_artifact_files=config.tools.max_artifact_files,
            artifact_root=paths.tool_artifacts,
            permission_policy=build_permission_policy(config),
            interactive=interactive,
            run_control=control,
            process_supervisor=supervisor,
            workspace=workspace,
            current_session_id=session_id,
        )

        stage = "web"
        web = start_web(config.web, registry, control)
        skill_manager = SkillManager(root)
        mcp_service = MCPService(config_file, logger, workspace_root=root)
        extension_tools = [ManageSkillTool(skill_manager), ConfigureMCPServerTool(mcp_service)]
        if not register_extension_tools(config, registry, system_prompt, extension_tools):
            system_prompt = build_system_prompt(
                interactive, skills=skill_meta or None, extension_management=False
            )
            notices.append(
                RuntimeNotice(
                    "extension_tools_omitted_context_limit",
                    "上下文窗口不足，未向模型注册扩展管理工具。",
                )
            )

        stage = "mcp"
        mcp, mcp_notices = start_mcp(
            config.mcp,
            registry,
            logger,
            artifact_root=paths.mcp_artifacts,
            stderr_root=paths.mcp_stderr,
            run_control=control,
            workspace_root=root,
        )
        notices.extend(mcp_notices)

        stage = "loop"

        def continue_check(iterations_used: int) -> bool:
            request = ContinueRequest(
                run_id=tool_context.current_run_id,
                session_id=tool_context.current_session_id,
                iterations_used=iterations_used,
                iteration_limit=config.agent.max_iterations,
            )
            try:
                decision = port.confirm_continue(request)
            except Exception:
                return False
            return decision.request_id == request.request_id and decision.continue_run

        loop = AgentLoop(
            config,
            LLMClient(config.active_provider),
            registry,
            tool_context,
            interactive=interactive,
            interrupt_check=interrupt_check,
            run_control=control,
            continue_check=continue_check if interactive else None,
            system_prompt=system_prompt,
        )
        return AgentRuntime(
            config=config,
            loop=loop,
            logger=logger,
            skill_store=skill_store,
            tool_context=tool_context,
            interaction=port,
            session_store=SessionStore(paths.sessions),
            visible_skills=visible_skills,
            notices=notices,
            skill_manager=skill_manager,
            mcp_service=mcp_service,
            web=web,
            mcp=mcp,
            run_store=RunStore(resolve_run_dir(config.agent.recovery.dir, root)),
            interactive=interactive,
            run_control=control,
            process_supervisor=supervisor,
            workspace=workspace,
        )
    except (RuntimeConfigError, RuntimeInitializationError):
        raise
    except BaseException as exc:
        if mcp is not None:
            mcp.close()
        if web is not None:
            web.close()
        if workspace is not None:
            workspace.close()
        else:
            supervisor.close()
        port.close()
        logger.session_end(reason="runtime_init_failed")
        raise RuntimeInitializationError(stage, f"Runtime 初始化失败（{stage}）：{exc}") from exc
