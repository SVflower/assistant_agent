"""Agent 对当前 Runtime 能力的安全、只读自省工具。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from assistant_agent.contracts.capabilities import MCPServerCapability
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.display import ToolDisplay
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.tool import Tool


class InspectRuntimeTool(Tool):
    name = "inspect_runtime"
    description = "列出当前 Runtime 的工具、Skill、MCP 状态和沙箱。能力查询必须用它，不搜索文件。"

    def __init__(
        self,
        *,
        sandbox: str,
        tool_names: Callable[[], Sequence[str]],
        skills: Callable[[], Sequence[tuple[str, str]]],
        mcp_servers: Callable[[], Sequence[MCPServerCapability]],
    ) -> None:
        self._sandbox = sandbox
        self._tool_names = tool_names
        self._skills = skills
        self._mcp_servers = mcp_servers
        self._last_snapshot: tuple[object, ...] | None = None

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tools = tuple(self._tool_names())
        skills = tuple(self._skills())
        servers = tuple(self._mcp_servers())
        server_count = len(servers)
        tool_count = sum(len(server.tool_names) for server in servers)
        snapshot = (self._sandbox, tools, skills, servers)
        repeated = snapshot == self._last_snapshot
        self._last_snapshot = snapshot
        lines = [f"当前 Runtime（sandbox={self._sandbox}）："]
        lines.append("工具：" + ("、".join(tools) if tools else "无"))
        lines.append(
            "Skills："
            + ("、".join(f"{name}（{source}）" for name, source in skills) if skills else "无")
        )
        lines.append(f"当前 MCP server：{server_count} 个；暴露工具：{tool_count} 个")
        safe_servers = []
        for server in servers:
            tool_names = list(server.tool_names)
            safe_servers.append(
                {
                    "name": server.name,
                    "transport": server.transport,
                    "startup": server.startup,
                    "status": server.status,
                    "tool_count": len(tool_names),
                    "tool_names": tool_names,
                }
            )
        if repeated:
            lines.append("本次结果与上次 inspect_runtime 查询相同，无需重复处理。")
        else:
            lines.append("MCP：" if servers else "MCP：无")
            for server in servers:
                tool_names = list(server.tool_names)
                detail = f"，工具：{'、'.join(tool_names)}" if tool_names else ""
                lines.append(
                    f"- {server.name}（{server.transport} / {server.startup} / "
                    f"{server.status}{detail}）"
                )
        metadata: dict[str, object] = {
            "sandbox": self._sandbox,
            "tools": list(tools),
            "tool_names": list(tools),
            "skills": [{"name": name, "source": source} for name, source in skills],
            "server_count": server_count,
            "tool_count": tool_count,
            "mcp_servers": safe_servers,
            "repeated": repeated,
        }
        return ToolResult.ok("\n".join(lines), metadata=metadata)

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return []

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay("查看 Runtime", "当前能力")
