"""模型可调用的 Skill 扩展管理工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from assistant_agent.config.schema import MCPServerConfig
from assistant_agent.config.writer import ConfigScope, ConfigWriteError
from assistant_agent.integrations.mcp.configure import MCPConfigureError, MCPService
from assistant_agent.integrations.skills.manager import SkillInstallError, SkillManager, SkillScope
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.display import ToolDisplay, safe_text
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.tool import Tool


class ManageSkillTool(Tool):
    name = "manage_skill"
    description = (
        "安装或卸载 Agent Skill。install 的 source 必须是本地 Skill 目录；"
        "默认安装到用户级专用目录，"
        "project scope 安装到 .agents/skills。安装后下次启动生效。"
    )

    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["install", "uninstall"]},
                "source": {"type": "string", "description": "install 的本地 Skill 目录"},
                "name": {"type": "string", "description": "uninstall 的 Skill 名"},
                "scope": {"type": "string", "enum": ["user", "project"]},
            },
            "required": ["action"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = args["action"]
        scope = cast(SkillScope, args.get("scope", "user"))
        try:
            if action == "install":
                source = args.get("source")
                if not source:
                    return ToolResult.error(
                        "install 缺少 source", code="invalid_arguments", executed=False
                    )
                result = self._manager.install(Path(str(source)), scope)
                verb = "已安装" if result.changed else "已存在"
                return ToolResult.ok(
                    f"{verb} Skill {result.name}（{scope}）：{result.path}\n下次启动生效。",
                    metadata={"name": result.name, "scope": scope, "changed": result.changed},
                )
            name = args.get("name")
            if not name:
                return ToolResult.error(
                    "uninstall 缺少 name", code="invalid_arguments", executed=False
                )
            self._manager.uninstall(str(name), scope)
            return ToolResult.ok(
                f"已卸载 Skill {name}（{scope}）。下次启动生效。",
                metadata={"name": name, "scope": scope, "changed": True},
            )
        except SkillInstallError as exc:
            return ToolResult.error(str(exc), code="skill_install_error")

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        scope = str(args.get("scope", "user"))
        name = args.get("name") or args.get("source") or "unknown"
        return [
            PermissionRequest(
                self.name,
                Capability.EXTENSION_MANAGE,
                f"skill/{scope}/{name}",
                "安装或卸载扩展会修改 Agent 的可执行指示来源",
            )
        ]

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        action = "安装 Skill" if args.get("action") == "install" else "卸载 Skill"
        return ToolDisplay(
            action,
            safe_text(args.get("source") or args.get("name") or "", 120),
            importance="external",
        )


class ConfigureMCPServerTool(Tool):
    name = "configure_mcp_server"
    description = (
        "列出、测试、添加、启停、信任或移除 MCP server 配置。"
        "用户询问当前有哪些 MCP 时使用 list；配置变更下次启动生效；"
        "env/headers 的敏感值必须写成 ${ENV_VAR}，不要传明文密钥。"
    )

    def __init__(self, service: MCPService) -> None:
        self._service = service

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "add",
                        "test",
                        "remove",
                        "enable",
                        "disable",
                        "trust",
                        "untrust",
                    ],
                },
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "scope": {"type": "string", "enum": ["user", "project"]},
                "server": {
                    "type": "object",
                    "description": (
                        "add/test 配置。stdio: {type,command,args,env,cwd}; "
                        "http: {type,url,headers}; 可选 timeout/enabled/max_tools。"
                    ),
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args["action"])
        scope = cast(ConfigScope, args.get("scope", "user"))
        try:
            if action == "list":
                configured = self._service.list()
                if args.get("scope"):
                    configured = {
                        name: item for name, item in configured.items() if item[0] == scope
                    }
                servers = [
                    {
                        "name": name,
                        "scope": item_scope,
                        "transport": server.type,
                        "startup": server.startup,
                        "enabled": server.enabled,
                        "trusted": server.auto_approve,
                    }
                    for name, (item_scope, server) in sorted(configured.items())
                ]
                if not servers:
                    return ToolResult.ok("当前未配置 MCP server。", metadata={"servers": []})
                lines = ["当前已配置 MCP server："]
                lines.extend(
                    f"- {item['name']}（{item['scope']} / {item['transport']} / "
                    f"{item['startup']} / {'enabled' if item['enabled'] else 'disabled'} / "
                    f"{'trusted' if item['trusted'] else 'approval-required'}）"
                    for item in servers
                )
                return ToolResult.ok("\n".join(lines), metadata={"servers": servers})
            name_value = args.get("name")
            if not name_value:
                return ToolResult.error(
                    f"{action} 缺少 name", code="invalid_arguments", executed=False
                )
            name = str(name_value)
            if action in {"add", "test"}:
                raw = args.get("server")
                if not isinstance(raw, dict):
                    return ToolResult.error(
                        f"{action} 缺少 server", code="invalid_arguments", executed=False
                    )
                server = MCPServerConfig.model_validate(raw)
                result = (
                    self._service.add(name, server, scope)
                    if action == "add"
                    else self._service.probe(name, server)
                )
                verb = "已验证并写入" if action == "add" else "验证通过"
                return ToolResult.ok(
                    f"{verb} MCP server {name}（发现 {len(result.tools)} 个工具）。"
                    + ("下次启动生效。" if action == "add" else ""),
                    metadata={"server": name, "scope": scope, "tools": list(result.tools)},
                )
            if action == "remove":
                if not self._service.remove(name, scope):
                    raise MCPConfigureError(f"{scope} scope 中不存在 MCP server：{name}")
            elif action in {"enable", "disable"}:
                self._service.set_enabled(name, action == "enable", scope)
            else:
                self._service.set_trusted(name, action == "trust", scope)
            return ToolResult.ok(
                f"MCP server {name} 已{_ACTION_LABEL[action]}（{scope}）。下次启动生效。",
                metadata={"server": name, "scope": scope, "action": action},
            )
        except (MCPConfigureError, ConfigWriteError, ValidationError, OSError) as exc:
            return ToolResult.error(str(exc), code="mcp_configure_error")

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        action = str(args["action"])
        if action == "list":
            return []
        name = str(args.get("name") or "unknown")
        scope = cast(ConfigScope, args.get("scope", "user"))
        requests: list[PermissionRequest] = []
        raw = args.get("server")
        if action in {"add", "test"} and isinstance(raw, dict):
            if raw.get("type", "stdio") == "http":
                requests.append(
                    PermissionRequest(
                        self.name,
                        Capability.NETWORK_ACCESS,
                        str(raw.get("url", "")),
                        "将连接远程 MCP server 并读取其工具清单",
                    )
                )
            else:
                command = " ".join(
                    [str(raw.get("command", "")), *map(str, raw.get("args", []))]
                ).strip()
                requests.append(
                    PermissionRequest(
                        self.name,
                        Capability.PROCESS_EXECUTE,
                        str(raw.get("command", name)),
                        "将启动第三方 MCP 子进程并读取其工具清单",
                        metadata={"display_target": command},
                    )
                )
        if action != "test":
            path = self._service.store.path(scope)
            requests.extend(
                [
                    PermissionRequest(
                        self.name,
                        Capability.FILESYSTEM_WRITE,
                        str(path),
                        "将原子修改 MCP 配置文件",
                    ),
                    PermissionRequest(
                        self.name,
                        Capability.EXTENSION_MANAGE,
                        f"mcp/{scope}/{name}/{action}",
                        "MCP 扩展可执行外部代码并影响后续 Agent 会话",
                    ),
                ]
            )
        return requests

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        action = _ACTION_LABEL.get(str(args.get("action")), "配置")
        return ToolDisplay(
            f"MCP {action}", safe_text(args.get("name", ""), 80), importance="external"
        )


_ACTION_LABEL = {
    "list": "列表",
    "add": "添加",
    "test": "测试",
    "remove": "移除",
    "enable": "启用",
    "disable": "禁用",
    "trust": "信任",
    "untrust": "取消信任",
}
