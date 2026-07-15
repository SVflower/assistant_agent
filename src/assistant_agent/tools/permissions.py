"""工具权限的稳定数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Capability(StrEnum):
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    PROCESS_EXECUTE = "process.execute"
    NETWORK_ACCESS = "network.access"
    MCP_CALL = "mcp.call"
    SKILL_LOAD = "skill.load"
    USER_INTERACTION = "user.interaction"


PermissionEffect = Literal["allow", "ask", "deny"]
PermissionMode = Literal["readonly", "workspace", "strict", "unrestricted"]


@dataclass(frozen=True)
class PermissionRequest:
    """一个 Tool 在执行前声明的单项能力请求。"""

    tool: str
    capability: Capability
    target: str
    risk: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def category(self) -> str:
        return f"{self.capability.value}:{self.tool}:{self.target}"

    @property
    def scope(self) -> PermissionScope:
        return PermissionScope(self.capability, self.tool, self.target)


@dataclass(frozen=True)
class PermissionScope:
    """会话授权的精确作用域，不跨 capability/tool/target 扩散。"""

    capability: Capability
    tool: str
    target: str


@dataclass(frozen=True)
class PermissionRule:
    effect: PermissionEffect
    capability: Capability
    target: str = "*"
    tool: str = "*"


@dataclass(frozen=True)
class PermissionDecision:
    effect: PermissionEffect
    reason: str
    matched_rule: PermissionRule | None = None
    remembered: bool = False
