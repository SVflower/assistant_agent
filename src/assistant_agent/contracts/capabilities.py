"""Runtime 的稳定、只读、脱敏能力快照。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RuntimeProfile = Literal["cli", "service", "web", "custom"]


class SandboxProfile(BaseModel):
    """一次 Run 固定使用的执行边界；创建后作为 checkpoint 的一部分保存。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    mode: Literal["off", "workspace", "container"]
    filesystem: Literal["host", "workspace", "read_only"]
    process: Literal["host", "confined", "container"]
    network: Literal["none", "bridge"]
    extensions: Literal["host", "disabled", "container"]
    containerized: bool
    resource_limits: dict[str, int | float | str] = Field(default_factory=dict)


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
    content_parts_version: int = 0
    input_modalities: tuple[Literal["text", "image"], ...] = ("text",)
    attachment_media_types: tuple[str, ...] = ()
    attachment_limits: dict[str, int | float] = field(default_factory=dict)
    filesystem_boundary: Literal["host", "workspace", "read_only"] = "host"
    process_boundary: Literal["host", "confined", "container"] = "host"
    network_boundary: Literal["none", "bridge"] = "none"
    containerized: bool = False
    extensions_isolated: bool = False
    resource_limits: dict[str, int | float | str] = field(default_factory=dict)

    def sandbox_profile(self) -> SandboxProfile:
        return SandboxProfile(
            mode=self.sandbox,
            filesystem=self.filesystem_boundary,
            process=self.process_boundary,
            network=self.network_boundary,
            extensions=("container" if self.extensions_isolated else "disabled"),
            containerized=self.containerized,
            resource_limits=dict(self.resource_limits),
        )
