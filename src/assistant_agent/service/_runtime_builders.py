"""Runtime 工厂的内部组件构建函数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from assistant_agent.agent.token_budget import estimate_message_tokens, estimate_tools_tokens
from assistant_agent.config.paths import project_skills_dir, user_skills_dir
from assistant_agent.config.schema import AppConfig, MCPConfig, SkillsConfig, WebConfig
from assistant_agent.contracts.errors import RuntimeDependencyError
from assistant_agent.mcp import MCPManager, MCPRequiredServerError
from assistant_agent.obs import NullLogger
from assistant_agent.runtime import (
    BaseWorkspace,
    ConfinedWorkspace,
    ContainerWorkspace,
    HostWorkspace,
    ProcessSupervisor,
    RunControl,
)
from assistant_agent.skills import SkillSource, SkillStore
from assistant_agent.tools.base import Tool
from assistant_agent.tools.permissions import Capability, PermissionRule
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.web import FetchURLTool, WebSearchTool
from assistant_agent.web import WebClient


@dataclass(frozen=True)
class RuntimeNotice:
    code: str
    message: str
    level: Literal["info", "warning"] = "warning"
    details: dict[str, object] = field(default_factory=dict)


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


def build_permission_policy(config: AppConfig) -> PermissionPolicy:
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


def start_web(cfg: WebConfig, registry: ToolRegistry, control: RunControl) -> WebClient | None:
    if not cfg.enabled:
        return None
    client = WebClient(cfg, run_control=control)
    try:
        registry.register(WebSearchTool(client))
        registry.register(FetchURLTool(client))
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
    run_control: RunControl,
    workspace_root: Path,
    allowed_transports: frozenset[str],
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
    )
    try:
        tools = manager.start()
        for tool in tools:
            registry.register(tool)
    except MCPRequiredServerError as exc:
        manager.close()
        raise RuntimeDependencyError(exc.server, exc.category, str(exc)) from exc
    except BaseException:
        manager.close()
        raise
    notices = [RuntimeNotice("mcp_warning", warning) for warning in manager.warnings]
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
    if tools:
        notices.append(
            RuntimeNotice(
                "mcp_tools_registered",
                f"已接入 {len(tools)} 个外部 MCP 工具。",
                level="info",
                details={"count": len(tools)},
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
