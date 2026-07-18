"""Runtime 的稳定、只读、脱敏能力快照。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MCPStatus = Literal[
    "disabled",
    "connected",
    "degraded_timeout",
    "degraded_connection",
    "degraded_discovery",
    "blocked_by_policy",
    "required_failed",
]


@dataclass(frozen=True)
class SkillCapability:
    name: str
    source: str
    fingerprint: str


@dataclass(frozen=True)
class MCPServerCapability:
    name: str
    transport: str
    startup: Literal["optional", "required"]
    status: MCPStatus
    tool_names: tuple[str, ...] = ()
    checked_at: str = ""
    error_category: str | None = None


@dataclass(frozen=True)
class RuntimeCapabilities:
    sandbox: Literal["off", "workspace", "container"]
    tools: tuple[str, ...]
    skills: tuple[SkillCapability, ...]
    mcp_servers: tuple[MCPServerCapability, ...]
    extension_management: bool
