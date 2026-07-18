"""MCP 启动状态与安全错误分类。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MCPStartup = Literal["optional", "required"]
MCPStatusCode = Literal[
    "disabled",
    "connected",
    "degraded_timeout",
    "degraded_connection",
    "degraded_discovery",
    "blocked_by_policy",
    "required_failed",
]


@dataclass(frozen=True)
class MCPServerStatus:
    name: str
    transport: str
    startup: MCPStartup
    status: MCPStatusCode
    tool_names: tuple[str, ...] = ()
    checked_at: str = ""
    error_category: str | None = None


class MCPRequiredServerError(RuntimeError):
    def __init__(self, server: str, category: str) -> None:
        self.server = server
        self.category = category
        super().__init__(f"必需 MCP server {server} 启动失败（{category}）")


def startup_failure_status(startup: MCPStartup, category: str) -> MCPStatusCode:
    if startup == "required":
        return "required_failed"
    if category == "timeout":
        return "degraded_timeout"
    if category == "discovery":
        return "degraded_discovery"
    if category == "policy":
        return "blocked_by_policy"
    return "degraded_connection"
