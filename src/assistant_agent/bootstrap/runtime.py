"""UI 无关的 Agent Runtime 工厂与资源生命周期。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.application.capabilities import RuntimePolicy, sandbox_satisfies
from assistant_agent.application.interactions import SafeDefaultInteractionPort
from assistant_agent.application.runtime import AgentRuntime
from assistant_agent.bootstrap.tools import (
    build_permission_policy,
    discover_skills,
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
from assistant_agent.mcp import MCPManager, MCPService
from assistant_agent.obs import create_logger, new_trace_id, sanitize_for_display
from assistant_agent.providers.litellm import LLMClient
from assistant_agent.runtime import BaseWorkspace, ProcessSupervisor, RunControl
from assistant_agent.session.run_store import RunStore
from assistant_agent.session.store import SessionStore
from assistant_agent.skills import LoadSkillTool, SkillManager
from assistant_agent.tools.artifacts import ArtifactStore
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.extensions import ConfigureMCPServerTool, ManageSkillTool
from assistant_agent.tools.registry import build_default_registry
from assistant_agent.web import WebClient


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
) -> AgentRuntime:
    """创建 UI 无关 Runtime；失败抛类型化异常并回滚所有已创建资源。"""
    port = interaction or SafeDefaultInteractionPort()
    policy = runtime_policy or RuntimePolicy.cli()
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
        if skills:
            registry.register(LoadSkillTool(skill_store))
        skill_meta = [(item.name, f"[{item.source}] {item.description}") for item in visible_skills]
        system_prompt = build_system_prompt(interactive, skills=skill_meta or None)

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
            confirm_dangerous_shell=config.tools.confirm_dangerous_shell,
            shell_timeout=config.tools.shell_timeout,
            interaction=port,
            sanitize_for_display=sanitize_for_display,
            max_output_chars=config.tools.max_output_chars,
            max_captured_output_chars=config.tools.max_captured_output_chars,
            max_artifact_files=config.tools.max_artifact_files,
            artifact_root=paths.tool_artifacts,
            permission_policy=build_permission_policy(config),
            interactive=interactive,
            current_session_id=session_id,
        )

        stage = "web"
        web = start_web(config.web, registry, control)
        skill_manager = SkillManager(root)
        mcp_service = MCPService(config_file, logger, workspace_root=root)
        extension_management = policy.allow_extension_management
        extension_tools = [ManageSkillTool(skill_manager), ConfigureMCPServerTool(mcp_service)]
        if not extension_management:
            system_prompt = build_system_prompt(
                interactive, skills=skill_meta or None, extension_management=False
            )
        elif not register_extension_tools(config, registry, system_prompt, extension_tools):
            extension_management = False
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
            allowed_transports=policy.allowed_mcp_transports,
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
                tool_names=item.tool_names,
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
            sanitize_for_display=sanitize_for_display,
            workspace=workspace,
            capabilities=capabilities,
        )
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
        if isinstance(exc, AgentServiceError):
            raise
        raise RuntimeInitializationError(stage, f"Runtime 初始化失败（{stage}）：{exc}") from exc
