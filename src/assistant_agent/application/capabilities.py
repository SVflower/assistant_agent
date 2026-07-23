"""调用方注入的 Runtime 能力上限。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assistant_agent.contracts.capabilities import RuntimeProfile

SandboxLevel = Literal["off", "workspace", "container"]
MCPTransport = Literal["stdio", "http"]
InputModality = Literal["text", "image"]


@dataclass(frozen=True)
class RuntimePolicy:
    """不可由 config、模型或请求放宽的部署策略。"""

    allow_extension_management: bool = True
    allow_personal_skills: bool = True
    allowed_mcp_transports: frozenset[MCPTransport] = frozenset({"stdio", "http"})
    minimum_sandbox: SandboxLevel = "off"
    profile: RuntimeProfile = "custom"
    allowed_tools: frozenset[str] | None = None
    auto_allow_tools: frozenset[str] = frozenset()
    allowed_input_modalities: frozenset[InputModality] = frozenset({"text", "image"})

    def __post_init__(self) -> None:
        invalid = self.allowed_mcp_transports - {"stdio", "http"}
        if invalid:
            raise ValueError(f"未知 MCP transport policy：{', '.join(sorted(invalid))}")
        if self.minimum_sandbox not in _SANDBOX_RANK:
            raise ValueError(f"未知 sandbox policy：{self.minimum_sandbox}")
        if self.profile not in {"cli", "service", "web", "custom"}:
            raise ValueError(f"未知 runtime profile：{self.profile}")
        if self.allowed_tools is not None and not self.auto_allow_tools <= self.allowed_tools:
            raise ValueError("auto_allow_tools 必须是 allowed_tools 的子集")
        if not self.allowed_input_modalities or not self.allowed_input_modalities <= {
            "text",
            "image",
        }:
            raise ValueError("allowed_input_modalities 不合法")

    def allows_tool(self, name: str) -> bool:
        return self.allowed_tools is None or name in self.allowed_tools

    @classmethod
    def cli(cls) -> RuntimePolicy:
        """保持现有本机 CLI 行为的兼容策略。"""
        return cls(profile="cli")

    @classmethod
    def service(cls) -> RuntimePolicy:
        """适合长期服务入口的保守默认策略。"""
        return cls(
            allow_extension_management=False,
            allow_personal_skills=False,
            allowed_mcp_transports=frozenset({"http"}),
            minimum_sandbox="workspace",
            profile="service",
        )

    @classmethod
    def web(cls) -> RuntimePolicy:
        """浏览器访问服务器 Agent 时使用的只读展示策略。"""
        tools = frozenset(
            {
                "ask_user",
                "inspect_runtime",
                "load_skill",
                "present_chart",
                "web_search",
            }
        )
        return cls(
            allow_extension_management=False,
            allow_personal_skills=False,
            allowed_mcp_transports=frozenset(),
            minimum_sandbox="workspace",
            profile="web",
            allowed_tools=tools,
            auto_allow_tools=tools,
        )


_SANDBOX_RANK: dict[SandboxLevel, int] = {"off": 0, "workspace": 1, "container": 2}


def sandbox_satisfies(actual: SandboxLevel, minimum: SandboxLevel) -> bool:
    return _SANDBOX_RANK[actual] >= _SANDBOX_RANK[minimum]
