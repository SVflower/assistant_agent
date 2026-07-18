"""调用方注入的 Runtime 能力上限。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SandboxLevel = Literal["off", "workspace", "container"]
MCPTransport = Literal["stdio", "http"]


@dataclass(frozen=True)
class RuntimePolicy:
    """不可由 config、模型或请求放宽的部署策略。"""

    allow_extension_management: bool = True
    allow_personal_skills: bool = True
    allowed_mcp_transports: frozenset[MCPTransport] = frozenset({"stdio", "http"})
    minimum_sandbox: SandboxLevel = "off"

    def __post_init__(self) -> None:
        invalid = self.allowed_mcp_transports - {"stdio", "http"}
        if invalid:
            raise ValueError(f"未知 MCP transport policy：{', '.join(sorted(invalid))}")
        if self.minimum_sandbox not in _SANDBOX_RANK:
            raise ValueError(f"未知 sandbox policy：{self.minimum_sandbox}")

    @classmethod
    def cli(cls) -> RuntimePolicy:
        """保持现有本机 CLI 行为的兼容策略。"""
        return cls()

    @classmethod
    def service(cls) -> RuntimePolicy:
        """适合长期服务入口的保守默认策略。"""
        return cls(
            allow_extension_management=False,
            allow_personal_skills=False,
            allowed_mcp_transports=frozenset({"http"}),
            minimum_sandbox="workspace",
        )


_SANDBOX_RANK: dict[SandboxLevel, int] = {"off": 0, "workspace": 1, "container": 2}


def sandbox_satisfies(actual: SandboxLevel, minimum: SandboxLevel) -> bool:
    return _SANDBOX_RANK[actual] >= _SANDBOX_RANK[minimum]
