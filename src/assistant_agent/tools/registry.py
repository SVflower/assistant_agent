"""工具注册表：管理可用工具，生成 schema，分发调用。"""

from __future__ import annotations

import time
from typing import Any

from assistant_agent.tools.ask import AskUserTool
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.file_ops import (
    EditFileTool,
    ListDirTool,
    MultiEditTool,
    ReadFileTool,
    WriteFileTool,
)
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
        """按名执行工具。未知工具或异常都归一为 ToolResult，不向外抛。

        执行前后计时，把工具调用作为结构化事件写入 ctx.logger（默认 NullLogger 无副作用）。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error(f"未知工具：{name}。可用工具：{', '.join(self.names())}")
        ctx._last_approval_wait_ms = None  # 清历史值，只认本次执行期间产生的等待
        start = time.perf_counter()
        try:
            result = tool.run(args, ctx)
        except Exception as exc:  # 工具实现的兜底，绝不让循环崩
            result = ToolResult.error(f"工具 {name} 执行异常：{exc}")
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        # 若本次执行中途等过用户确认，从总耗时里剥离，duration_ms 只反映实际执行。
        approval_wait_ms = ctx._last_approval_wait_ms
        ctx._last_approval_wait_ms = None  # 用后即清，绝不残留到下一次工具调用
        duration_ms = elapsed_ms - approval_wait_ms if approval_wait_ms else elapsed_ms

        # 单次输出截断：防单个大输出吞噬本地模型上下文。截断前先记原始长度到审计。
        limit = ctx.max_tool_output_chars
        truncated = limit > 0 and len(result.output) > limit
        ctx.logger.tool_call(
            name=name,
            args=args,
            duration_ms=max(duration_ms, 0),
            status="error" if result.is_error else "ok",
            output=result.output,  # 传原始输出，output_len 记原始长度
            approval_wait_ms=approval_wait_ms,
            truncated=truncated,
        )
        if truncated:
            dropped = len(result.output) - limit
            result = ToolResult(
                output=result.output[:limit] + f"\n…（已截断 {dropped} 字符，可缩小范围重试）",
                is_error=result.is_error,
            )
        return result


def build_default_registry() -> ToolRegistry:
    """构建带内置工具的注册表：文件四件套 + 代码检索 + git 只读 + 用户澄清。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(MultiEditTool())
    registry.register(ListDirTool())
    registry.register(ShellTool())
    registry.register(CodeSearchTool())
    registry.register(GitTool())
    registry.register(AskUserTool())
    return registry
