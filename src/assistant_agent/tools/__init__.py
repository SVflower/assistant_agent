"""工具系统：基类、注册表与内置工具。"""

from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.declarative import FunctionTool, PermissionResolver, agent_tool
from assistant_agent.tools.registry import ToolRegistry, build_default_registry

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
