"""工具基类与公共类型。

工具是 Agent 的扩展点：新增能力 = 写一个 Tool 子类并注册，内核循环不变。
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolContext:
    """工具执行时可用的运行时上下文。

    把"是否需要确认危险操作""超时"等设置和确认回调注入工具，
    使工具本身不直接依赖配置或 UI。
    """

    confirm_dangerous_shell: bool = True
    shell_timeout: int = 60
    # 危险操作确认回调：返回 True 表示用户允许。默认拒绝（安全优先）。
    confirm: Callable[[str], bool] = lambda _msg: False


@dataclass
class ToolResult:
    """工具执行结果。"""

    output: str
    is_error: bool = False

    @classmethod
    def ok(cls, output: str) -> ToolResult:
        return cls(output=output, is_error=False)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(output=message, is_error=True)


class Tool(abc.ABC):
    """所有工具的基类。"""

    #: 工具名，模型用它来调用。须唯一、稳定。
    name: str = ""
    #: 给模型看的描述，决定模型何时调用。要清晰，对"笨模型"也友好。
    description: str = ""

    @property
    @abc.abstractmethod
    def parameters(self) -> dict[str, Any]:
        """返回 JSON Schema 形式的参数定义（OpenAI function 的 parameters 部分）。"""

    @abc.abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。实现应自行处理异常并返回 ToolResult，不要向外抛。"""

    def to_schema(self) -> dict[str, Any]:
        """转成 OpenAI function-calling 的 tool schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
