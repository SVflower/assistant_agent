"""工具系统公共入口。"""

from importlib import import_module
from typing import Any

from assistant_agent.tools.declarative import FunctionTool, PermissionResolver, agent_tool
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry, build_default_registry
from assistant_agent.tools.tool import Tool


def __getattr__(name: str) -> Any:
    if name == "ToolContext":
        return import_module("assistant_agent.tools.base").ToolContext
    raise AttributeError(name)


__all__ = [
    "FunctionTool",
    "PermissionResolver",
    "Tool",
    "ToolResult",
    "ToolContext",
    "ToolRegistry",
    "agent_tool",
    "build_default_registry",
]
