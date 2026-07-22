"""Runtime 的稳定、只读、脱敏能力快照。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RuntimeProfile = Literal["cli", "service", "web", "custom"]

RuntimeStartupPhase = Literal[
    "loading_config",
    "starting_workspace",
    "discovering_skills",
    "starting_web",
    "preparing_mcp",
    "creating_loop",
    "ready",
]

RuntimeStartupStatus = Literal["started", "completed", "failed"]

MCPStatus = Literal[
    "disabled",
    "discovering",
    "available_cached",
    "restart_required",
    "connecting",
    "connected",
    "degraded_timeout",
    "degraded_connection",
    "degraded_discovery",
    "blocked_by_policy",
    "required_failed",
]


@dataclass(frozen=True)
class RuntimeStartupEvent:
    phase: RuntimeStartupPhase
    status: RuntimeStartupStatus
    message: str = ""


@dataclass(frozen=True)
class RuntimeNotice:
    code: str
    message: str
    level: Literal["info", "warning"] = "warning"
    details: dict[str, object] = field(default_factory=dict)


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
    profile: RuntimeProfile = "custom"
    chart_spec_versions: tuple[int, ...] = ()
