"""工具系统公共入口。"""

from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.declarative import FunctionTool, PermissionResolver, agent_tool
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry, build_default_registry
from assistant_agent.tools.tool import Tool

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
