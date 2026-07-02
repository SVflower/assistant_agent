"""工具注册表：管理可用工具，生成 schema，分发调用。"""

from __future__ import annotations

from typing import Any

from assistant_agent.tools.ask import AskUserTool
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.file_ops import ListDirTool, ReadFileTool, WriteFileTool
from assistant_agent.tools.git import GitTool
from assistant_agent.tools.search import CodeSearchTool
from assistant_agent.tools.shell import ShellTool


class ToolRegistry:
    """工具集合。负责注册、按名查找、生成给模型的 schema、执行调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"工具缺少 name：{tool!r}")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的 OpenAI function-calling schema。"""
        return [tool.to_schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按名执行工具。未知工具或异常都归一为 ToolResult，不向外抛。"""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error(f"未知工具：{name}。可用工具：{', '.join(self.names())}")
        try:
            return tool.run(args, ctx)
        except Exception as exc:  # 工具实现的兜底，绝不让循环崩
            return ToolResult.error(f"工具 {name} 执行异常：{exc}")


def build_default_registry() -> ToolRegistry:
    """构建带内置工具的注册表：文件四件套 + 代码检索 + git 只读 + 用户澄清。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ListDirTool())
    registry.register(ShellTool())
    registry.register(CodeSearchTool())
    registry.register(GitTool())
    registry.register(AskUserTool())
    return registry
