"""git 只读工具：status / diff / log / show / branch。

安全设计：
- 子命令白名单，只允许只读操作；写操作（commit/reset/push/checkout 等）拒绝。
- 用 shell=False + 列表参数执行，从根上杜绝 shell 注入（args 经 shlex 解析，不过 shell）。
- 只读，无副作用，因此不需要危险操作确认。
"""

from __future__ import annotations

import shlex
from typing import Any

from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.process import format_process_result
from assistant_agent.tools.shell_policy import shell_permission_requests

# 只读子命令白名单
_ALLOWED = {"status", "diff", "log", "show", "branch"}


class GitTool(Tool):
    name = "git"
    description = (
        "执行只读 git 命令，理解工作区与变更。"
        "支持 status/diff/log/show/branch；不支持写操作（commit/reset/push 等会被拒绝）。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "enum": sorted(_ALLOWED),
                    "description": "git 子命令（只读）",
                },
                "args": {
                    "type": "string",
                    "description": "附加参数，如 diff 的文件路径、log 的 -n 5",
                },
            },
            "required": ["subcommand"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        subcommand = args.get("subcommand")
        if not subcommand:
            return ToolResult.error("缺少参数 subcommand")
        if subcommand not in _ALLOWED:
            allowed = ", ".join(sorted(_ALLOWED))
            return ToolResult.error(
                f"不支持的 git 子命令：{subcommand}（只读工具，仅允许：{allowed}）"
            )

        try:
            extra = shlex.split(args.get("args") or "")
        except ValueError as exc:
            return ToolResult.error(f"参数解析失败：{exc}")

        # log 默认限制条数，避免全量历史刷屏
        if subcommand == "log" and not any(a.startswith("-n") or a == "--max-count" for a in extra):
            extra = ["-n", "20", *extra]

        # 关闭分页器，否则非交互环境可能卡住
        cmd = ["git", "--no-pager", subcommand, *extra]
        try:
            completed = ctx.execute_process(
                cmd,
                shell=False,
                timeout=ctx.shell_timeout,
                max_stream_chars=ctx.max_captured_output_chars,
            )
        except FileNotFoundError:
            return ToolResult.error(
                "未找到 git，可执行文件不在 PATH 中",
                code="process_start_failed",
                retryable=True,
            )
        except OSError as exc:
            return ToolResult.error(
                f"无法执行 git：{exc}", code="process_start_failed", retryable=True
            )

        if completed.timed_out:
            return ToolResult.error(
                f"git 命令超时（>{ctx.shell_timeout}s）",
                code="timeout",
                retryable=True,
                metadata={"timed_out": True},
            )
        if completed.interrupted:
            cancelled = completed.termination_reason.value == "cancelled"
            return ToolResult.error(
                "git 命令已强制取消" if cancelled else "git 命令已暂停",
                code="cancelled" if cancelled else "interrupted",
                retryable=False,
                metadata={"termination_reason": completed.termination_reason.value},
            )
        output, artifacts, metadata = format_process_result(
            completed,
            artifact_writer=ctx.write_artifact,
            artifact_prefix="git-output",
            inline_limit=ctx.max_output_chars,
        )
        # 非零退出（如非 git 仓库）不当工具错误，交模型判断
        return ToolResult.ok(output, metadata=metadata, artifacts=artifacts)

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        subcommand = str(args.get("subcommand") or "<missing>")
        extra = str(args.get("args") or "").strip()
        target = f"git {subcommand}{' ' + extra if extra else ''}"
        return shell_permission_requests(target, self.name)
