"""UI 无关的 Agent Runtime 工厂与资源生命周期。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from assistant_agent.agent.context.window import estimate_message_tokens
from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.application.capabilities import RuntimePolicy, sandbox_satisfies
from assistant_agent.application.interactions import SafeDefaultInteractionPort
from assistant_agent.application.runtime import AgentRuntime
from assistant_agent.bootstrap.tools import (
    bounded_skill_metadata,
    build_permission_policy,
    discover_skills,
    register_core_tool_if_fits,
    register_extension_tools,
    start_mcp,
    start_web,
    start_workspace,
)
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.paths import resolve_log_dir, resolve_run_dir, state_paths
from assistant_agent.config.schema import AppConfig
from assistant_agent.contracts.capabilities import (
    MCPServerCapability,
    RuntimeCapabilities,
    RuntimeNotice,
    RuntimeStartupEvent,
    RuntimeStartupPhase,
    SkillCapability,
)
from assistant_agent.contracts.errors import (
    AgentServiceError,
    RuntimeConfigError,
    RuntimeInitializationError,
    RuntimePolicyError,
)
from assistant_agent.contracts.failures import ContinuationPrompt, ContinuationResult
from assistant_agent.contracts.interactions import ContinueRequest, InteractionPort
from assistant_agent.execution import (
    BaseWorkspace,
    ManagedProcessRegistry,
    ProcessSupervisor,
    RunControl,
)
from assistant_agent.integrations.mcp import MCPManager, MCPService
from assistant_agent.integrations.skills import LoadSkillTool, SkillManager
from assistant_agent.integrations.web_access import WebClient
from assistant_agent.observability import create_logger, new_trace_id, sanitize_for_display
from assistant_agent.persistence.artifacts import ArtifactStore
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.providers.litellm import LLMClient
from assistant_agent.tools.charts import PresentChartTool
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.extensions import ConfigureMCPServerTool, ManageSkillTool
from assistant_agent.tools.processes import ManageProcessTool
from assistant_agent.tools.registry import ToolRegistry, build_default_registry
from assistant_agent.tools.runtime_inspection import InspectRuntimeTool
from assistant_agent.tools.tool import Tool


def _register_policy_tool(
    policy: RuntimePolicy,
    config: AppConfig,
    registry: ToolRegistry,
    system_prompt: str,
    tool: Tool,
    *,
    enabled: bool = True,
) -> bool:
    if not enabled or not policy.allows_tool(tool.name):
        return False
    return register_core_tool_if_fits(config, registry, system_prompt, tool)


def _configure_chart_tool(
    policy: RuntimePolicy,
    config: AppConfig,
    registry: ToolRegistry,
    system_prompt: str,
    *,
    interactive: bool,
    skills: list[tuple[str, str]],
) -> tuple[str, bool, list[RuntimeNotice]]:
    available = _register_policy_tool(
        policy,
        config,
        registry,
        system_prompt,
        PresentChartTool(),
        enabled=config.agent.recovery.enabled,
    )
    if available:
        return system_prompt, True, []
    prompt = build_system_prompt(
        interactive,
        skills=skills or None,
        managed_process=False,
        chart_presentation=False,
        runtime_profile=policy.profile,
    )
    if not policy.allows_tool("present_chart"):
        return prompt, False, []
    notice = RuntimeNotice(
        (
            "chart_presentation_omitted_context_limit"
            if config.agent.recovery.enabled
            else "chart_presentation_requires_recovery"
        ),
        (
            "上下文窗口不足，当前 Runtime 未注册图表展示工具。"
            if config.agent.recovery.enabled
            else "图表展示要求启用 Run checkpoint，当前 Runtime 未注册该工具。"
        ),
    )
    return prompt, False, [notice]


def _managed_process_notices(policy: RuntimePolicy, available: bool) -> list[RuntimeNotice]:
    if available or not policy.allows_tool("manage_process"):
        return []
    return [
        RuntimeNotice(
            "managed_process_omitted_context_limit",
            "上下文窗口不足，当前 Runtime 未注册后台进程管理工具。",
        )
    ]


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
    runtime_policy: RuntimePolicy | None = None,
    startup_observer: Callable[[RuntimeStartupEvent], None] | None = None,
) -> AgentRuntime:
    """创建 UI 无关 Runtime；失败抛类型化异常并回滚所有已创建资源。"""
    port = interaction or SafeDefaultInteractionPort()
    policy = runtime_policy or RuntimePolicy.cli()
    config_file = Path(config_path).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve()
    try:
        with _startup_stage(startup_observer, "loading_config", "正在读取配置"):
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
        try:
            config = AppConfig.model_validate(config.model_dump(mode="python"))
        except ValueError as exc:
            port.close()
            raise RuntimeConfigError(str(exc)) from exc
    if max_iterations is not None:
        if max_iterations < 1:
            port.close()
            raise RuntimeConfigError("max_iterations 必须大于 0")
        config.agent.max_iterations = max_iterations
    if not sandbox_satisfies(config.sandbox.mode, policy.minimum_sandbox):
        port.close()
        raise RuntimePolicyError(
            f"sandbox.mode={config.sandbox.mode} 低于调用方要求 {policy.minimum_sandbox}"
        )

    control = run_control or RunControl()
    supervisor = ProcessSupervisor()
    process_manager = ManagedProcessRegistry(
        max_processes=config.tools.max_background_processes,
        max_stream_chars=config.tools.max_background_output_chars,
    )
    registry = build_default_registry(policy.allowed_tools)
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
        with _startup_stage(startup_observer, "starting_workspace", "正在准备工作区"):
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
        with _startup_stage(startup_observer, "discovering_skills", "正在发现 Skills"):
            skill_store = discover_skills(
                config.skills, root, allow_personal=policy.allow_personal_skills
            )
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
        if skills and policy.allows_tool("load_skill"):
            registry.register(LoadSkillTool(skill_store))
        elif skills:
            visible_skills = []
        skill_meta, omitted_skills = bounded_skill_metadata(
            visible_skills, config.skills, config.agent.max_context_tokens
        )
        if omitted_skills:
            notices.append(
                RuntimeNotice(
                    "skills_catalog_truncated",
                    "Skill 元数据目录超过上下文预算，部分 Skill 未注入初始提示词。",
                    details={"skills": omitted_skills, "count": len(omitted_skills)},
                )
            )
        system_prompt = build_system_prompt(
            interactive,
            skills=skill_meta or None,
            managed_process=False,
            runtime_profile=policy.profile,
        )

        system_prompt, chart_available, chart_notices = _configure_chart_tool(
            policy,
            config,
            registry,
            system_prompt,
            interactive=interactive,
            skills=skill_meta,
        )
        notices.extend(chart_notices)

        tool_context = ToolContext(
            workspace=workspace,
            run_control=control,
            logger=logger,
            artifact_store=ArtifactStore(
                root,
                max_chars=config.tools.max_captured_output_chars,
                max_files=config.tools.max_artifact_files,
                root=paths.tool_artifacts,
            ),
            process_manager=process_manager,
            confirm_dangerous_shell=config.tools.confirm_dangerous_shell,
            shell_timeout=config.tools.shell_timeout,
            interaction=port,
            sanitize_for_display=sanitize_for_display,
            max_output_chars=config.tools.max_output_chars,
            max_captured_output_chars=config.tools.max_captured_output_chars,
            max_artifact_files=config.tools.max_artifact_files,
            artifact_root=paths.tool_artifacts,
            permission_policy=build_permission_policy(
                config, trusted_tools=policy.auto_allow_tools
            ),
            interactive=interactive,
            current_session_id=session_id,
        )

        stage = "web"
        with _startup_stage(startup_observer, "starting_web", "正在准备网络工具"):
            web = start_web(config.web, registry, control, allowed_tools=policy.allowed_tools)
        skill_manager = SkillManager(root)
        mcp_service = MCPService(config_file, logger, workspace_root=root)
        inspection_tool = InspectRuntimeTool(
            sandbox=config.sandbox.mode,
            tool_names=lambda: registry.names(),
            skills=lambda: [(item.name, item.source) for item in visible_skills],
            mcp_servers=lambda: mcp.server_capabilities() if mcp is not None else (),
        )
        inspection_available = _register_policy_tool(
            policy, config, registry, system_prompt, inspection_tool
        )
        if not inspection_available:
            system_prompt = build_system_prompt(
                interactive,
                skills=skill_meta or None,
                runtime_inspection=False,
                managed_process=False,
                chart_presentation=chart_available,
                runtime_profile=policy.profile,
            )
            if policy.allows_tool("inspect_runtime"):
                notices.append(
                    RuntimeNotice(
                        "runtime_inspection_omitted_context_limit",
                        "上下文窗口不足，当前 Runtime 未注册能力自省工具。",
                    )
                )
        managed_process_prompt = build_system_prompt(
            interactive,
            skills=skill_meta or None,
            runtime_inspection=inspection_available,
            managed_process=True,
            chart_presentation=chart_available,
            runtime_profile=policy.profile,
        )
        managed_process_available = _register_policy_tool(
            policy, config, registry, managed_process_prompt, ManageProcessTool()
        )
        if managed_process_available:
            system_prompt = managed_process_prompt
        notices.extend(_managed_process_notices(policy, managed_process_available))
        extension_tools: list[Tool] = [
            tool
            for tool in (ManageSkillTool(skill_manager), ConfigureMCPServerTool(mcp_service))
            if policy.allows_tool(tool.name)
        ]
        extension_management = policy.allow_extension_management and bool(extension_tools)
        if not extension_management:
            system_prompt = build_system_prompt(
                interactive,
                skills=skill_meta or None,
                extension_management=False,
                runtime_inspection=inspection_available,
                managed_process=managed_process_available,
                chart_presentation=chart_available,
                runtime_profile=policy.profile,
            )
        elif not register_extension_tools(config, registry, system_prompt, extension_tools):
            extension_management = False
            system_prompt = build_system_prompt(
                interactive,
                skills=skill_meta or None,
                extension_management=False,
                runtime_inspection=inspection_available,
                managed_process=managed_process_available,
                chart_presentation=chart_available,
                runtime_profile=policy.profile,
            )
            notices.append(
                RuntimeNotice(
                    "extension_tools_omitted_context_limit",
                    "上下文窗口不足，未向模型注册扩展管理工具。",
                )
            )

        stage = "mcp"
        with _startup_stage(startup_observer, "preparing_mcp", "正在准备 MCP 扩展"):
            mcp, mcp_notices = start_mcp(
                config.mcp,
                registry,
                logger,
                artifact_root=paths.mcp_artifacts,
                stderr_root=paths.mcp_stderr,
                catalog_root=paths.mcp_catalog,
                run_control=control,
                workspace_root=root,
                allowed_transports=policy.allowed_mcp_transports,
                max_tools_schema_tokens=max(
                    config.agent.max_context_tokens
                    - config.agent.reserved_output_tokens
                    - estimate_message_tokens({"role": "system", "content": system_prompt})
                    - 5,
                    0,
                ),
                allowed_tools=policy.allowed_tools,
            )
        notices.extend(mcp_notices)

        stage = "loop"

        def budget_continue_check(prompt: ContinuationPrompt) -> ContinuationResult:
            request = ContinueRequest(
                run_id=tool_context.current_run_id,
                session_id=tool_context.current_session_id,
                iterations_used=prompt.used if prompt.resource == "iterations" else 0,
                iteration_limit=prompt.limit if prompt.resource == "iterations" else 0,
                reason=prompt.reason,
                resource=prompt.resource,
                used=prompt.used,
                limit=prompt.limit,
                suggested_increment=prompt.suggested_increment,
                hard_limit=prompt.hard_limit,
                extension_count=prompt.extension_count,
                max_extensions=prompt.max_extensions,
            )
            try:
                decision = port.confirm_continue(request)
            except Exception:
                return ContinuationResult(request.request_id)
            return ContinuationResult(
                request.request_id,
                decision.request_id == request.request_id and decision.continue_run,
            )

        summary_client = None
        if config.agent.compaction.enabled and config.agent.compaction.summary_model:
            summary_provider = config.providers.get(config.agent.compaction.summary_model)
            if summary_provider is not None:
                summary_client = LLMClient(summary_provider)

        with _startup_stage(startup_observer, "creating_loop", "正在创建 Agent Runtime"):
            loop = AgentLoop(
                config,
                LLMClient(config.active_provider),
                registry,
                tool_context,
                interactive=interactive,
                interrupt_check=interrupt_check,
                run_control=control,
                budget_continue_check=budget_continue_check if interactive else None,
                system_prompt=system_prompt,
                summary_client=summary_client,
            )
        skill_capabilities = tuple(
            SkillCapability(
                name=item.name,
                source=item.source,
                fingerprint=hashlib.sha256(item.path.read_bytes()).hexdigest()[:12],
            )
            for item in visible_skills
        )
        mcp_capabilities = tuple(
            MCPServerCapability(
                name=item.name,
                transport=item.transport,
                startup=item.startup,
                status=item.status,
                tool_names=tuple(name for name in item.tool_names if policy.allows_tool(name)),
                checked_at=item.checked_at,
                error_category=item.error_category,
            )
            for item in (mcp.server_statuses() if mcp is not None else ())
        )
        capabilities = RuntimeCapabilities(
            sandbox=config.sandbox.mode,
            tools=tuple(registry.names()),
            skills=skill_capabilities,
            mcp_servers=mcp_capabilities,
            extension_management=extension_management,
            profile=policy.profile,
        )
        runtime = AgentRuntime(
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
            execution_leases=FileSessionExecutionLeaseManager(paths.workspace / "execution-leases"),
            interactive=interactive,
            run_control=control,
            process_supervisor=supervisor,
            process_manager=process_manager,
            sanitize_for_display=sanitize_for_display,
            workspace=workspace,
            capabilities=capabilities,
        )
        _emit_startup(startup_observer, RuntimeStartupEvent("ready", "completed", "Agent 已就绪"))
        return runtime
    except BaseException as exc:
        process_manager.close()
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
        if isinstance(exc, AgentServiceError):
            raise
        raise RuntimeInitializationError(stage, f"Runtime 初始化失败（{stage}）：{exc}") from exc


def _emit_startup(
    observer: Callable[[RuntimeStartupEvent], None] | None,
    event: RuntimeStartupEvent,
) -> None:
    if observer is None:
        return
    try:
        observer(event)
    except Exception:
        pass


@contextmanager
def _startup_stage(
    observer: Callable[[RuntimeStartupEvent], None] | None,
    phase: RuntimeStartupPhase,
    message: str,
):
    _emit_startup(observer, RuntimeStartupEvent(phase, "started", message))
    try:
        yield
    except BaseException:
        _emit_startup(observer, RuntimeStartupEvent(phase, "failed", message))
        raise
    else:
        _emit_startup(observer, RuntimeStartupEvent(phase, "completed", message))
