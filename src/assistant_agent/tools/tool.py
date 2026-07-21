"""内置和扩展工具共享的抽象基类。"""

from __future__ import annotations

import abc
from typing import Any

from assistant_agent.contracts.events import ToolDisplay
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.display import call_display, result_display
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest


class Tool(abc.ABC):
    name: str = ""
    description: str = ""

    @property
    @abc.abstractmethod
    def parameters(self) -> dict[str, Any]:
        """返回 OpenAI function-calling 使用的 JSON Schema。"""

    @abc.abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具并返回结构化结果。"""

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return call_display(self.name, args)

    def display_result(self, args: dict[str, Any], result: ToolResult) -> ToolDisplay:
        return result_display(self.name, args, result, self.display_call(args))

    def argument_validation_error(
        self,
        message: str,
        metadata: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """允许工具把通用 JSON Schema 错误收敛为领域内可修复错误。"""
        return ToolResult.error(
            message,
            code="invalid_arguments",
            retryable=True,
            metadata=metadata,
            executed=False,
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return [
            PermissionRequest(
                tool=self.name,
                capability=Capability.PROCESS_EXECUTE,
                target=self.name or "unknown",
                risk="未知扩展工具可能产生外部副作用",
            )
        ]

    def replay_policy(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
        requests: list[PermissionRequest],
    ) -> ReplayPolicy | None:
        """工具可显式声明恢复策略；默认交由 Registry 保守推断。"""
        return None
