"""运行时装配（Runtime）。

把配置加载、客户端、工具注册表、技能、MCP、日志、循环的组装从 main 抽出来（还 D7）。
Runtime 是上下文管理器，统管 logger 会话与 MCP 线程/子进程的生命周期——退出时干净关闭。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import typer

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.agent.token_budget import estimate_message_tokens, estimate_tools_tokens
from assistant_agent.config.loader import ConfigError, find_config_file, load_config
from assistant_agent.config.paths import (
    project_skills_dir,
    resolve_log_dir,
    resolve_run_dir,
    state_paths,
    user_skills_dir,
)
from assistant_agent.config.schema import AppConfig, MCPConfig, SkillsConfig, WebConfig
from assistant_agent.llm.client import LLMClient
from assistant_agent.mcp import MCPManager, MCPService
from assistant_agent.obs import NullLogger, create_logger, new_trace_id
from assistant_agent.runtime import (
    BaseWorkspace,
    ConfinedWorkspace,
    HostWorkspace,
    ProcessSupervisor,
    RunControl,
)
from assistant_agent.session.run_store import RunStore
from assistant_agent.skills import LoadSkillTool, SkillManager, SkillMeta, SkillSource, SkillStore
from assistant_agent.tools.base import Tool, ToolContext
from assistant_agent.tools.extensions import ConfigureMCPServerTool, ManageSkillTool
from assistant_agent.tools.permissions import Capability, PermissionRequest, PermissionRule
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.registry import ToolRegistry, build_default_registry
from assistant_agent.tools.web import FetchURLTool, WebSearchTool
from assistant_agent.ui.console import Console
from assistant_agent.web import WebClient


def _discover_skills(cfg: SkillsConfig) -> SkillStore:
    """按配置发现技能。禁用时返回空 store（不扫描、不注入）。

    默认目录：./.agents/skills（项目级）与 ~/.assistant_agent/skills（个人级）；
    旧 ./.assistant_agent/skills 最低优先级只读兼容。
    """
    if not cfg.enabled:
        return SkillStore({})
    if cfg.dirs:
        dirs = [Path(d).expanduser() for d in cfg.dirs]
        sources: list[SkillSource] = ["configured"] * len(dirs)
    else:
        dirs = [
            project_skills_dir(),
            user_skills_dir(),
            Path.cwd() / ".assistant_agent" / "skills",
        ]
        sources = ["project", "personal", "legacy"]
    return SkillStore.discover(
        dirs,
        sources=sources,
        trusted_names=set(cfg.trusted_project_skills),
    )


def _build_permission_policy(config: AppConfig) -> PermissionPolicy:
    return PermissionPolicy(
        mode=config.permissions.mode,
        rules=[
            PermissionRule(
                effect=rule.effect,
                capability=Capability(rule.capability),
                target=rule.target,
                tool=rule.tool,
            )
            for rule in config.permissions.rules
        ],
        sensitive_paths=config.permissions.sensitive_paths or None,
    )


def _authorize_skills(skills: list[SkillMeta], ctx: ToolContext) -> list[SkillMeta]:
    """项目/自定义 Skill 元数据进入 prompt 前先做一次聚合会话授权。"""
    visible = [meta for meta in skills if meta.trusted]
    requests = [
        PermissionRequest(
            "load_skill",
            Capability.SKILL_LOAD,
            f"{meta.source}/{meta.name}",
            "Skill 名称、描述和正文来自当前项目或自定义目录，会影响模型行为",
            metadata={"source": meta.source, "trusted": False},
        )
        for meta in skills
        if not meta.trusted
    ]
    if requests and ctx.request_permissions(requests):
        ctx.permission_grants.update(request.scope for request in requests)
        visible.extend(meta for meta in skills if not meta.trusted)
    return sorted(visible, key=lambda meta: meta.name)


def _start_mcp(
    cfg: MCPConfig,
    console: Console,
    registry: ToolRegistry,
    *,
    artifact_root: Path | None = None,
    stderr_root: Path | None = None,
    run_control: RunControl | None = None,
) -> MCPManager | None:
    """连接 MCP server、注册其工具、打印警告。禁用或无 server 时返回 None。"""
    if not cfg.enabled or not cfg.servers:
        return None
    manager = MCPManager(
        cfg,
        NullLogger(),
        artifact_root=artifact_root,
        stderr_root=stderr_root,
        run_control=run_control,
    )
    try:
        tools = manager.start()
        for tool in tools:
            registry.register(tool)
    except BaseException:
        manager.close()
        raise
    for warning in manager.warnings:
        console.error(f"（MCP）{warning}")
    trusted_servers = [
        name for name, server in cfg.servers.items() if server.enabled and server.auto_approve
    ]
    if trusted_servers:
        console.error(
            "（MCP）高风险：已信任整个 server，当前工具调用将自动放行："
            + ", ".join(sorted(trusted_servers))
        )
    if tools:
        console.info(f"（MCP）已接入 {len(tools)} 个外部工具。")
    return manager


def _start_web(
    cfg: WebConfig, registry: ToolRegistry, run_control: RunControl | None = None
) -> WebClient | None:
    """构造 Web client 并注册工具；不在启动阶段发起网络请求。"""
    if not cfg.enabled:
        return None
    client = WebClient(cfg, run_control=run_control)
    try:
        registry.register(WebSearchTool(client))
        registry.register(FetchURLTool(client))
    except BaseException:
        client.close()
        raise
    return client


def _register_extension_tools(
    config: AppConfig,
    registry: ToolRegistry,
    system_prompt: str,
    tools: list[Tool],
) -> bool:
    """仅在固定开销仍留有消息空间时向模型暴露扩展管理工具。"""
    schemas = [*registry.schemas(), *(tool.to_schema() for tool in tools)]
    fixed = (
        estimate_message_tokens({"role": "system", "content": system_prompt})
        + estimate_tools_tokens(schemas)
        + config.agent.reserved_output_tokens
    )
    if fixed + 5 > config.agent.max_context_tokens:
        return False
    for tool in tools:
        registry.register(tool)
    return True


@dataclass
class Runtime:
    """一次运行的装配结果 + 生命周期管理。用作上下文管理器。"""

    config: AppConfig
    loop: AgentLoop
    logger: NullLogger
    skill_store: SkillStore
    visible_skills: list[SkillMeta] = field(default_factory=list)
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

    def skills_meta(self) -> list[tuple[str, str]]:
        return [(m.name, f"[{m.source}] {m.description}") for m in self.visible_skills]

    def new_run(self, task: str, session_id: str | None = None) -> RunCoordinator | None:
        if not self.config.agent.recovery.enabled:
            return None
        return RunCoordinator.create(
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

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self, reason: str = "") -> None:
        """关闭 MCP（线程/子进程）并记 session_end。幂等。"""
        if self._closed:
            return
        self._closed = True
        if self.mcp is not None:
            self.mcp.close()
        if self.web is not None:
            self.web.close()
        if self.workspace is not None:
            self.workspace.close()
        else:
            self.process_supervisor.close()
        self.logger.session_end(reason=reason)


def build_runtime(
    config_path: Path | None,
    console: Console,
    *,
    interactive: bool,
    interrupt_check: Callable[[], bool] | None = None,
    run_control: RunControl | None = None,
    provider: str | None = None,
    max_iterations: int | None = None,
) -> Runtime:
    """加载配置并装配整个运行时。失败时打印错误并退出（typer.Exit）。

    interactive：True 为 chat 多轮（允许澄清），False 为 run 单次（遇歧义自行假设）。
    provider：非空则覆盖 active（临时后端，不改文件）。max_iterations：非空覆盖最大轮数。
    """
    try:
        resolved_config_path = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else find_config_file()
        )
        config = load_config(config_path)
    except ConfigError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    if provider is not None:
        if provider not in config.providers:
            available = ", ".join(sorted(config.providers))
            console.error(f"未知 provider：{provider}。可选：{available}")
            raise typer.Exit(code=1)
        config.active = provider
    if max_iterations is not None:
        config.agent.max_iterations = max_iterations
    if not config.tools.confirm_dangerous_shell:
        console.error(
            "tools.confirm_dangerous_shell=false 已废弃，不能关闭统一权限边界；"
            "需要宽松模式请显式配置 permissions.mode: unrestricted。"
        )

    client = LLMClient(config.active_provider)
    control = run_control or RunControl()
    process_supervisor = ProcessSupervisor()
    if config.sandbox.mode == "workspace":
        workspace: BaseWorkspace = ConfinedWorkspace(
            Path.cwd(), supervisor=process_supervisor, control=control
        )
    elif config.sandbox.mode == "off":
        workspace = HostWorkspace(Path.cwd(), supervisor=process_supervisor, control=control)
    else:
        raise RuntimeError("container sandbox 尚未完成初始化")
    registry = build_default_registry()

    # 先发现 Skill，但不把未信任项目元数据暴露给模型。
    skill_store = _discover_skills(config.skills)
    skill_manager = SkillManager()
    paths = state_paths()
    logging_config = config.logging.model_copy(
        update={"dir": str(resolve_log_dir(config.logging.dir))}
    )
    logger = create_logger(logging_config, new_trace_id())
    if resolved_config_path is None:  # load_config 成功时理论上不可达
        raise RuntimeError("无法确定当前配置文件路径")
    mcp_service = MCPService(resolved_config_path, logger, workspace_root=Path.cwd())
    logger.bind_session(None)
    run_store = RunStore(resolve_run_dir(config.agent.recovery.dir))
    mcp: MCPManager | None = None
    web: WebClient | None = None
    try:
        logger.session_start(
            provider=config.active,
            model=config.active_provider.model,
            mode="chat" if interactive else "run",
            cwd=str(Path.cwd()),
        )

        tool_ctx = ToolContext(
            confirm_dangerous_shell=config.tools.confirm_dangerous_shell,
            shell_timeout=config.tools.shell_timeout,
            confirm=console.confirm,
            confirm_scoped=getattr(console, "confirm_scoped", None),
            ask=console.ask_question,
            logger=logger,
            max_output_chars=config.tools.max_output_chars,
            max_captured_output_chars=config.tools.max_captured_output_chars,
            max_artifact_files=config.tools.max_artifact_files,
            artifact_root=paths.tool_artifacts,
            permission_policy=_build_permission_policy(config),
            interactive=interactive,
            run_control=control,
            process_supervisor=process_supervisor,
            workspace=workspace,
        )

        skills = skill_store.list()
        visible_skills = _authorize_skills(skills, tool_ctx)
        if skills:
            registry.register(LoadSkillTool(skill_store))
        skill_meta = [(meta.name, f"[{meta.source}] {meta.description}") for meta in visible_skills]
        system_prompt = build_system_prompt(interactive, skills=skill_meta or None)

        web = _start_web(config.web, registry, control)
        extension_tools = [ManageSkillTool(skill_manager), ConfigureMCPServerTool(mcp_service)]
        if not _register_extension_tools(config, registry, system_prompt, extension_tools):
            system_prompt = build_system_prompt(
                interactive,
                skills=skill_meta or None,
                extension_management=False,
            )
            console.error(
                "（扩展管理）当前上下文窗口不足，模型管理工具未注册；"
                "仍可使用 /skills 和 /mcp 命令。"
            )
        # MCP（M7b）：连接 server、注册外部工具。禁用/无 server 时 None。
        mcp = _start_mcp(
            config.mcp,
            console,
            registry,
            artifact_root=paths.mcp_artifacts,
            stderr_root=paths.mcp_stderr,
            run_control=control,
        )
        continue_check = console.confirm_continue if interactive else None
        loop = AgentLoop(
            config,
            client,
            registry,
            tool_ctx,
            interactive=interactive,
            interrupt_check=interrupt_check,
            run_control=control,
            continue_check=continue_check,
            system_prompt=system_prompt,
        )
        console.set_show_reasoning(config.ui.show_reasoning)
        console.set_display_mode(config.ui.display_mode)
        console.set_context_limit(config.agent.max_context_tokens)
        console.banner(config.active, config.active_provider.model, config.permissions.mode)
        return Runtime(
            config=config,
            loop=loop,
            logger=logger,
            skill_store=skill_store,
            visible_skills=visible_skills,
            skill_manager=skill_manager,
            mcp_service=mcp_service,
            web=web,
            mcp=mcp,
            run_store=run_store,
            interactive=interactive,
            run_control=control,
            process_supervisor=process_supervisor,
            workspace=workspace,
        )
    except BaseException:
        if mcp is not None:
            mcp.close()
        if web is not None:
            web.close()
        workspace.close()
        logger.session_end(reason="runtime_init_failed")
        raise
