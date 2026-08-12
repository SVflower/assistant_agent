"""Runtime 工厂的内部组件构建函数。"""

from __future__ import annotations

from pathlib import Path

from assistant_agent.agent.context.window import estimate_message_tokens, estimate_tools_tokens
from assistant_agent.config.paths import project_skills_dir, user_skills_dir
from assistant_agent.config.schema import AppConfig, MCPConfig, SkillsConfig, WebConfig
from assistant_agent.contracts.capabilities import RuntimeNotice
from assistant_agent.contracts.errors import RuntimeDependencyError
from assistant_agent.execution import (
    BaseWorkspace,
    ConfinedWorkspace,
    ContainerWorkspace,
    HostWorkspace,
    ProcessSupervisor,
    RunControl,
)
from assistant_agent.integrations.mcp import MCPManager, MCPRequiredServerError
from assistant_agent.integrations.mcp.catalog import MCPToolCatalog
from assistant_agent.integrations.skills import SkillSource, SkillStore
from assistant_agent.integrations.web_access import WebClient
from assistant_agent.observability import NullLogger
from assistant_agent.tools.permissions import Capability, PermissionRule
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.tool import Tool
from assistant_agent.tools.web import FetchURLTool, WebSearchTool


def discover_skills(
    cfg: SkillsConfig, workspace_root: Path, *, allow_personal: bool = True
) -> SkillStore:
    if not cfg.enabled:
        return SkillStore({})
    sources: list[SkillSource]
    if cfg.dirs:
        dirs = []
        for item in cfg.dirs:
            configured = Path(item).expanduser()
            dirs.append(
                configured.resolve()
                if configured.is_absolute()
                else (workspace_root / configured).resolve()
            )
        sources = ["configured"] * len(dirs)
    else:
        dirs = [project_skills_dir(workspace_root)]
        sources = ["project"]
        if allow_personal:
            dirs.append(user_skills_dir())
            sources.append("personal")
        dirs.append(workspace_root / ".assistant_agent" / "skills")
        sources.append("legacy")
    return SkillStore.discover(
        dirs,
        sources=sources,
        trusted_names=set(cfg.trusted_project_skills),
    )


def bounded_skill_metadata(
    skills: list,
    cfg: SkillsConfig,
    max_context_tokens: int,
) -> tuple[list[tuple[str, str]], list[str]]:
    """生成初始 Skill 目录；正文仍由 load_skill 渐进披露。"""
    budget = min(cfg.catalog_max_chars, max(int(max_context_tokens * 0.02) * 4, 256))
    selected: list[tuple[str, str]] = []
    omitted: list[str] = []
    used = 0
    for item in sorted(skills, key=lambda value: value.name):
        prefix = f"[{item.source}] "
        available = budget - used - len(item.name) - len(prefix) - 4
        if available <= 0:
            omitted.append(item.name)
            continue
        description = item.description
        if len(description) > available:
            if available < 16:
                omitted.append(item.name)
                continue
            description = description[: available - 1] + "…"
        selected.append((item.name, prefix + description))
        used += len(item.name) + len(prefix) + len(description) + 4
    return selected, omitted


def build_permission_policy(
    config: AppConfig, *, trusted_tools: frozenset[str] = frozenset()
) -> PermissionPolicy:
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
        trusted_tools=trusted_tools,
    )


def start_workspace(
    config: AppConfig,
    root: Path,
    control: RunControl,
    supervisor: ProcessSupervisor,
) -> tuple[BaseWorkspace, list[RuntimeNotice]]:
    if config.sandbox.mode == "off":
        return HostWorkspace(root, supervisor=supervisor, control=control), []
    if config.sandbox.mode == "workspace":
        return ConfinedWorkspace(root, supervisor=supervisor, control=control), []
    workspace = ContainerWorkspace(
        root,
        supervisor=supervisor,
        control=control,
        engine=config.sandbox.engine,
        image=config.sandbox.image,
        network=config.sandbox.network,
        memory=config.sandbox.memory,
        cpus=config.sandbox.cpus,
        pids_limit=config.sandbox.pids_limit,
        user=config.sandbox.user,
    )
    host_mcp = sorted(
        name for name, server in config.mcp.servers.items() if config.mcp.enabled and server.enabled
    )
    return workspace, [
        RuntimeNotice(
            "container_host_capabilities",
            "Shell/Git 在容器内运行；Web 与外部 MCP 仍在宿主机运行。",
            details={"mcp_servers": host_mcp},
        )
    ]


def start_web(
    cfg: WebConfig,
    registry: ToolRegistry,
    control: RunControl,
    *,
    allowed_tools: frozenset[str] | None = None,
) -> WebClient | None:
    selected = {"web_search", "fetch_url"}
    if not cfg.enabled or (allowed_tools is not None and not selected & allowed_tools):
        return None
    client = WebClient(cfg, run_control=control)
    try:
        for tool in (WebSearchTool(client), FetchURLTool(client)):
            if allowed_tools is None or tool.name in allowed_tools:
                registry.register(tool)
    except BaseException:
        client.close()
        raise
    return client


def start_mcp(
    cfg: MCPConfig,
    registry: ToolRegistry,
    logger: NullLogger,
    *,
    artifact_root: Path,
    stderr_root: Path,
    catalog_root: Path,
    run_control: RunControl,
    workspace_root: Path,
    allowed_transports: frozenset[str],
    max_tools_schema_tokens: int,
    allowed_tools: frozenset[str] | None = None,
) -> tuple[MCPManager | None, list[RuntimeNotice]]:
    if not cfg.servers:
        return None, []
    manager = MCPManager(
        cfg,
        logger,
        artifact_root=artifact_root,
        stderr_root=stderr_root,
        run_control=run_control,
        workspace_root=workspace_root,
        allowed_transports=allowed_transports,
        catalog=MCPToolCatalog(catalog_root),
    )
    try:
        tools = manager.start_runtime()
        registered: list = []
        omitted: list[str] = []
        for tool in tools:
            if allowed_tools is not None and tool.name not in allowed_tools:
                continue
            server = cfg.servers.get(tool.server_name)
            schemas = [*registry.schemas(), tool.to_schema()]
            if (
                server is not None
                and server.startup == "optional"
                and estimate_tools_tokens(schemas) > max_tools_schema_tokens
            ):
                omitted.append(tool.name)
                continue
            registry.register(tool)
            registered.append(tool)
        manager.restrict_optional_runtime_tools(registered)
    except MCPRequiredServerError as exc:
        manager.close()
        raise RuntimeDependencyError(exc.server, exc.category, str(exc)) from exc
    except BaseException:
        manager.close()
        raise
    notices = [RuntimeNotice("mcp_warning", warning) for warning in manager.warnings]
    if omitted:
        notices.append(
            RuntimeNotice(
                "mcp_tools_omitted_context_limit",
                "上下文窗口不足，部分 optional MCP 工具未注册到当前 Runtime。",
                details={"tools": omitted, "count": len(omitted)},
            )
        )
    discovering = [item.name for item in manager.server_statuses() if item.status == "discovering"]
    if discovering:
        notices.append(
            RuntimeNotice(
                "mcp_catalog_discovery_background",
                "部分 optional MCP 正在后台发现工具目录；不会阻塞 Agent。",
                level="info",
                details={"servers": discovering},
            )
        )
    trusted = sorted(
        name for name, server in cfg.servers.items() if server.enabled and server.auto_approve
    )
    if trusted:
        notices.append(
            RuntimeNotice(
                "mcp_server_auto_approved",
                "已显式信任整个 MCP server，其工具调用将按配置自动放行。",
                details={"servers": trusted},
            )
        )
    if registered:
        registered_servers = sorted({tool.server_name for tool in registered})
        notices.append(
            RuntimeNotice(
                "mcp_tools_registered",
                f"已接入 {len(registered_servers)} 个 MCP Server。",
                level="info",
                details={
                    "count": len(registered_servers),
                    "server_count": len(registered_servers),
                    "tool_count": len(registered),
                    "servers": registered_servers,
                },
            )
        )
    return manager, notices


def register_extension_tools(
    config: AppConfig,
    registry: ToolRegistry,
    system_prompt: str,
    tools: list[Tool],
) -> bool:
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


def register_core_tool_if_fits(
    config: AppConfig,
    registry: ToolRegistry,
    system_prompt: str,
    tool: Tool,
) -> bool:
    schemas = [*registry.schemas(), tool.to_schema()]
    fixed = (
        estimate_message_tokens({"role": "system", "content": system_prompt})
        + estimate_tools_tokens(schemas)
        + config.agent.reserved_output_tokens
    )
    if fixed + 5 > config.agent.max_context_tokens:
        return False
    registry.register(tool)
    return True
