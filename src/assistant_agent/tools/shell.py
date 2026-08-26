"""有界输出的 shell 命令工具。"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
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
        "任意命令仍经过统一权限策略。git sparse-checkout 必须使用 "
        "git -C <独立仓库目录> sparse-checkout，不能依赖 cd 切换目录。"
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
        if not getattr(ctx.workspace, "writable", True):
            return ToolResult.error(
                "当前 Run 是只读工作区，不允许执行 Shell。",
                code="filesystem_read_only",
                executed=False,
            )
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.error(
                "缺少参数 command", code="invalid_arguments", retryable=True, executed=False
            )
        sparse_error = _validate_sparse_checkout_target(command, ctx)
        if sparse_error is not None:
            return sparse_error
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
                metadata={
                    "timed_out": True,
                    "execution_duration_ms": completed.execution_duration_ms,
                    "drain_duration_ms": completed.drain_duration_ms,
                    "cleanup_duration_ms": completed.cleanup_duration_ms,
                },
            )
        if completed.background_process:
            return ToolResult.error(
                "检测到前台命令遗留后台进程；受管进程树已终止。"
                "需要跨步骤运行服务时，请使用当前 Runtime 提供的 manage_process。",
                code="background_process_detected",
                retryable=False,
                metadata={
                    "termination_reason": completed.termination_reason.value,
                    "execution_duration_ms": completed.execution_duration_ms,
                    "drain_duration_ms": completed.drain_duration_ms,
                    "cleanup_duration_ms": completed.cleanup_duration_ms,
                },
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


def _validate_sparse_checkout_target(command: str, ctx: ToolContext) -> ToolResult | None:
    """阻止目录切换失败时 sparse-checkout 意外修改宿主仓库。"""
    if "sparse-checkout" not in command.lower():
        return None
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = []
    tokens = [_unquote(token) for token in tokens]
    if (
        len(tokens) < 5
        or Path(tokens[0]).name.lower() not in {"git", "git.exe"}
        or tokens[1] != "-C"
        or tokens[3].lower() != "sparse-checkout"
    ):
        return ToolResult.error(
            "已拒绝可能污染当前项目的 sparse checkout。请先克隆到独立目录，再使用 "
            "git -C <独立仓库目录> sparse-checkout set <子目录>；不要使用 cd ... && git。",
            code="unsafe_git_repository_target",
            retryable=True,
            executed=False,
        )
    try:
        repository = ctx.resolve_path(tokens[2])
    except (OSError, ValueError):
        repository = Path(tokens[2]).expanduser().resolve()
    workspace_root = ctx.workspace_root.resolve()
    if repository == workspace_root or not (repository / ".git").is_dir():
        return ToolResult.error(
            "已拒绝 sparse checkout：-C 必须指向已存在的独立 Git 克隆目录，"
            "不能指向当前 Agent 工作区。",
            code="unsafe_git_repository_target",
            retryable=True,
            executed=False,
        )
    return None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


__all__ = ["ShellTool", "is_dangerous"]
