"""当前 Runtime 拥有的后台进程工具。"""

from __future__ import annotations

import json
import re
from typing import Any

from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.ports import ManagedProcessSnapshotPort
from assistant_agent.tools.shell_policy import shell_permission_requests
from assistant_agent.tools.tool import Tool

_DETACH_SYNTAX = re.compile(
    r'(?i)(?:^|\s)(?:nohup|disown)(?:\s|$)|(?:^|\s)start(?:\s+"[^"]*")?\s+/b'
    r"(?:\s|$)|&amp;|&\s*$"
)
_PROCESS_ID = re.compile(r"^proc-[a-f0-9]{12}$")


class ManageProcessTool(Tool):
    name = "manage_process"
    description = (
        "管理当前 Runtime 拥有的后台进程。action=start 启动跨步骤服务；"
        "status/logs/list 查询；stop 停止。command 中不要使用 start /b、nohup、disown 或 &。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "status", "logs", "stop", "list"],
                },
                "command": {"type": "string", "minLength": 1},
                "process_id": {"type": "string", "pattern": r"^proc-[a-f0-9]{12}$"},
                "cwd": {"type": "string", "description": "可选工作目录"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        manager = ctx.process_manager
        if manager is None:
            return ToolResult.error(
                "当前 Runtime 不支持受管后台进程。",
                code="managed_process_unavailable",
                executed=False,
            )
        action = args.get("action")
        try:
            if action == "start":
                return self._start(args, ctx)
            if action == "list":
                snapshots = manager.list()
                output = (
                    "没有受管后台进程。"
                    if not snapshots
                    else "\n".join(_summary(item) for item in snapshots)
                )
                return ToolResult.ok(output, metadata={"count": len(snapshots)})

            process_id = args.get("process_id")
            if not isinstance(process_id, str) or not _PROCESS_ID.fullmatch(process_id):
                return _invalid(f"action={action} 需要有效 process_id")
            if action == "stop":
                snapshot = manager.stop(process_id)
                return ToolResult.ok(_summary(snapshot), metadata=_metadata(snapshot))
            if action in {"status", "logs"}:
                snapshot = manager.get(process_id)
                output = _logs(snapshot) if action == "logs" else _summary(snapshot)
                return ToolResult.ok(output, metadata=_metadata(snapshot))
            return _invalid("未知 action")
        except OSError:
            return ToolResult.error(
                "后台进程无法启动或管理。",
                code="managed_process_error",
                retryable=True,
            )
        except RuntimeError as exc:
            code = str(getattr(exc, "code", "managed_process_error"))
            return ToolResult.error(str(exc), code=code, retryable=code != "managed_process_closed")

    def _start(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.workspace.backend == "container":
            return ToolResult.error(
                "容器 Workspace 暂不支持跨步骤后台进程。",
                code="managed_process_container_unsupported",
                executed=False,
            )
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _invalid("action=start 需要 command")
        if _DETACH_SYNTAX.search(command):
            return _invalid("后台命令不得再次使用 start /b、nohup、disown 或 &")
        cwd = ctx.resolve_path(args.get("cwd") or ctx.workspace_root)
        assert ctx.process_manager is not None
        snapshot = ctx.process_manager.start(command, cwd=str(cwd))
        return ToolResult.ok(_summary(snapshot), metadata=_metadata(snapshot))

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        action = args.get("action")
        if action == "start" and isinstance(args.get("command"), str):
            return shell_permission_requests(args["command"], self.name)
        if action == "stop" and isinstance(args.get("process_id"), str):
            process_id = args["process_id"]
            return [
                PermissionRequest(
                    self.name,
                    Capability.PROCESS_EXECUTE,
                    process_id,
                    "停止当前 Runtime 拥有的后台进程",
                    metadata={"display_target": process_id},
                )
            ]
        return []


def _invalid(message: str) -> ToolResult:
    return ToolResult.error(message, code="invalid_arguments", retryable=True, executed=False)


def _metadata(snapshot: ManagedProcessSnapshotPort) -> dict[str, Any]:
    return {
        "process_id": snapshot.process_id,
        "status": snapshot.status,
        "returncode": snapshot.returncode,
        "elapsed_seconds": round(snapshot.elapsed_seconds, 3),
        "stdout_bytes": snapshot.stdout.total_bytes,
        "stderr_bytes": snapshot.stderr.total_bytes,
        "error_code": snapshot.error_code,
    }


def _summary(snapshot: ManagedProcessSnapshotPort) -> str:
    return json.dumps(_metadata(snapshot), ensure_ascii=False, sort_keys=True)


def _logs(snapshot: ManagedProcessSnapshotPort) -> str:
    parts = [_summary(snapshot)]
    if snapshot.stdout.text:
        parts.append(f"stdout:\n{snapshot.stdout.text.rstrip()}")
    if snapshot.stderr.text:
        parts.append(f"stderr:\n{snapshot.stderr.text.rstrip()}")
    return "\n".join(parts)


__all__ = ["ManageProcessTool"]
