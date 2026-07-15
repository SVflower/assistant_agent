"""工具注册表：管理可用工具，生成 schema，分发调用。"""

from __future__ import annotations

import copy
import time
from typing import Any

from assistant_agent.tools.ask import AskUserTool
from assistant_agent.tools.base import Tool, ToolBudget, ToolContext, ToolResult
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

_TRUNCATION_SUFFIX = "\n…（输出已截断，可缩小范围重试）"


def _truncate_output(output: str, limit: int) -> str:
    """把输出限制在 limit 内，且截断标记本身也计入限制。"""
    if limit <= 0:
        return ""
    if len(output) <= limit:
        return output
    if limit <= len(_TRUNCATION_SUFFIX):
        return _TRUNCATION_SUFFIX[:limit]
    return output[: limit - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


def _budget_error(reason: str, budget: ToolBudget) -> ToolResult:
    if reason == "max_tool_calls":
        message = (
            "未执行工具：任务工具调用预算已耗尽"
            f"（{budget.used_calls}/{budget.max_calls}）。"
            "请缩小任务范围或提高 agent.max_tool_calls。"
        )
    else:
        message = (
            "未执行工具：任务累计工具输出预算已耗尽"
            f"（{budget.used_output_chars}/{budget.max_total_output_chars} 字符）。"
            "请缩小任务范围或提高 agent.max_total_tool_output_chars。"
        )
    return ToolResult(
        output=message,
        is_error=True,
        budget_exhausted=reason,
        executed=False,
    )


def _limit_result_output(result: ToolResult, ctx: ToolContext) -> tuple[ToolResult, str, bool]:
    """应用单次/累计输出限制，并返回（新结果、原始输出、是否截断）。"""
    original_output = result.output
    limits = [limit for limit in (ctx.max_output_chars,) if limit > 0]
    remaining = ctx.budget.remaining_output_chars() if ctx.budget is not None else None
    if remaining is not None:
        limits.append(remaining)
    effective_limit = min(limits) if limits else None
    if effective_limit is not None and len(original_output) > effective_limit:
        returned_output = _truncate_output(original_output, effective_limit)
        truncated = True
    else:
        returned_output = original_output
        truncated = False

    budget_exhausted = result.budget_exhausted
    if (
        truncated
        and remaining is not None
        and effective_limit == remaining
        and len(original_output) > remaining
    ):
        budget_exhausted = "max_total_tool_output_chars"
    if ctx.budget is not None:
        ctx.budget.consume_output(len(returned_output))

    return (
        ToolResult(
            output=returned_output,
            is_error=result.is_error,
            budget_exhausted=budget_exhausted,
            executed=result.executed,
        ),
        original_output,
        truncated,
    )


def _finish_denied(
    name: str,
    args: dict[str, Any],
    result: ToolResult,
    ctx: ToolContext,
    start: float,
) -> ToolResult:
    """统一收尾未执行的权限拒绝，并保留可审计 tool_call 事件。"""
    wall_duration_ms = int((time.perf_counter() - start) * 1000)
    approval_wait_ms = ctx.consume_approval_wait()
    limited, original_output, truncated = _limit_result_output(result, ctx)
    ctx.logger.tool_call(
        name=name,
        args=args,
        duration_ms=max(wall_duration_ms - approval_wait_ms, 0),
        status="denied",
        output=original_output,
        approval_wait_ms=approval_wait_ms or None,
        truncated=truncated,
        wall_duration_ms=wall_duration_ms,
        execution_duration_ms=max(wall_duration_ms - approval_wait_ms, 0),
        returned_output_len=len(limited.output),
    )
    return limited


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
            result = ToolResult.error(f"未知工具：{name}。可用工具：{', '.join(self.names())}")
            limited, _, _ = _limit_result_output(result, ctx)
            return limited
        ctx.reset_approval_wait()
        start = time.perf_counter()
        try:
            requests = tool.permission_requests(args, ctx)
            for pre_observer in ctx.pre_tool_observers:
                reason = pre_observer.pre_tool_use(
                    name, copy.deepcopy(args), copy.deepcopy(requests)
                )
                if reason:
                    result = ToolResult(
                        output=f"[permission_denied] 权限拒绝：{reason}",
                        is_error=True,
                        executed=False,
                    )
                    return _finish_denied(name, args, result, ctx, start)
        except Exception as exc:
            result = ToolResult(
                output=f"[permission_denied] 权限检查失败，已拒绝执行：{exc}",
                is_error=True,
                executed=False,
            )
            return _finish_denied(name, args, result, ctx, start)

        if not ctx.request_permissions(requests):
            result = ToolResult(
                output="[permission_denied] 权限拒绝：工具动作未获授权",
                is_error=True,
                executed=False,
            )
            return _finish_denied(name, args, result, ctx, start)

        if ctx.budget is not None:
            exhausted = ctx.budget.try_consume_call()
            if exhausted is not None:
                return _budget_error(exhausted, ctx.budget)

        try:
            result = tool.run(args, ctx)
        except Exception as exc:  # 工具实现的兜底，绝不让循环崩
            result = ToolResult.error(f"工具 {name} 执行异常：{exc}")
        wall_duration_ms = int((time.perf_counter() - start) * 1000)
        approval_wait_ms = ctx.consume_approval_wait()
        execution_duration_ms = max(wall_duration_ms - approval_wait_ms, 0)

        result, original_output, truncated = _limit_result_output(result, ctx)

        for post_observer in ctx.post_tool_observers:
            try:
                post_observer.post_tool_use(
                    name,
                    copy.deepcopy(args),
                    copy.deepcopy(requests),
                    copy.deepcopy(result),
                )
            except Exception as exc:
                # Post observer 只观察；失败不能覆盖真实工具结果。
                ctx.logger.observer_error(phase="post", tool=name, error=str(exc))

        ctx.logger.tool_call(
            name=name,
            args=args,
            duration_ms=execution_duration_ms,
            status="error" if result.is_error else "ok",
            output=original_output,
            approval_wait_ms=approval_wait_ms or None,
            truncated=truncated,
            wall_duration_ms=wall_duration_ms,
            execution_duration_ms=execution_duration_ms,
            returned_output_len=len(result.output),
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
