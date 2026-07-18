"""内置和扩展工具共享的抽象基类。"""

from __future__ import annotations

import abc
from typing import Any

from assistant_agent.contracts.events import ToolDisplay
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.display import call_display, result_display
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
