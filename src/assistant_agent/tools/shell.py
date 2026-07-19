"""有界输出的 shell 命令工具。"""

from __future__ import annotations

from typing import Any

from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.process_output import format_process_result
from assistant_agent.tools.shell_policy import shell_permission_requests
from assistant_agent.tools.tool import Tool


def is_dangerous(command: str) -> bool:
    return len(shell_permission_requests(command)) > 1


class ShellTool(Tool):
    name = "run_shell"
    description = (
        "执行一条 shell 命令并返回有界 stdout/stderr。超大输出保存为 workspace artifact；"
        "任意命令仍经过统一权限策略。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "description": "完整 shell 命令"}
            },
            "required": ["command"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.error(
                "缺少参数 command", code="invalid_arguments", retryable=True, executed=False
            )
        try:
            completed = ctx.execute_process(
                command,
                shell=True,
                timeout=ctx.shell_timeout,
                max_stream_chars=ctx.max_captured_output_chars,
            )
        except OSError as exc:
            return ToolResult.error(
                f"无法执行命令：{exc}", code="process_start_failed", retryable=True
            )
        if completed.timed_out:
            return ToolResult.error(
                f"命令超时（>{ctx.shell_timeout}s）：{command}。进程已终止。",
                code="timeout",
                retryable=True,
                metadata={"timed_out": True},
            )
        if completed.interrupted:
            cancelled = completed.termination_reason.value == "cancelled"
            return ToolResult.error(
                "命令已强制取消" if cancelled else "命令已暂停并终止受管进程树",
                code="cancelled" if cancelled else "interrupted",
                retryable=False,
                metadata={"termination_reason": completed.termination_reason.value},
            )
        output, artifacts, metadata = format_process_result(
            completed,
            artifact_writer=ctx.write_artifact,
            artifact_prefix="shell-output",
            inline_limit=ctx.max_output_chars,
        )
        return ToolResult.ok(output, metadata=metadata, artifacts=artifacts)

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return []
        return shell_permission_requests(command, self.name)


__all__ = ["ShellTool", "is_dangerous"]
