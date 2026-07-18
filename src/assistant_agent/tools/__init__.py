"""工具系统公共入口。"""

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    exports = {
        "FunctionTool": ("assistant_agent.tools.declarative", "FunctionTool"),
        "PermissionResolver": ("assistant_agent.tools.declarative", "PermissionResolver"),
        "Tool": ("assistant_agent.tools.tool", "Tool"),
        "ToolContext": ("assistant_agent.tools.base", "ToolContext"),
        "ToolRegistry": ("assistant_agent.tools.registry", "ToolRegistry"),
        "ToolResult": ("assistant_agent.tools.models", "ToolResult"),
        "agent_tool": ("assistant_agent.tools.declarative", "agent_tool"),
        "build_default_registry": (
            "assistant_agent.tools.registry",
            "build_default_registry",
        ),
    }
    target = exports.get(name)
    if target is not None:
        module, attribute = target
        return getattr(import_module(module), attribute)
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
