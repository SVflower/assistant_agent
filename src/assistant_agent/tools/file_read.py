"""有界文件读取与目录浏览工具。"""

from __future__ import annotations

import heapq
from typing import Any

from assistant_agent.execution import WorkspaceError
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.file_io import path_request
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.tool import Tool

_DEFAULT_PAGE_LINES = 2_000
_MAX_PAGE_LINES = 5_000
_MAX_READ_CHARS = 100_000
_DEFAULT_DIR_RESULTS = 200
_MAX_DIR_RESULTS = 1_000


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "按行读取 UTF-8 文本文件。可用 start_line/end_line 读取大文件的指定范围；"
        "行号从 1 开始，结果会提示总行数和下一页。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "description": "文件路径"},
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "起始行（1-based），默认 1",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "结束行（包含），默认最多读取 2000 行",
                },
            },
            "required": ["path"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve_path(args["path"])
        except WorkspaceError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        if not path.exists():
            return ToolResult.error(f"文件不存在：{path}", code="not_found", retryable=True)
        if not path.is_file():
            return ToolResult.error(f"不是文件：{path}", code="not_file", retryable=True)

        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", start + _DEFAULT_PAGE_LINES - 1))
        explicit_range = "start_line" in args or "end_line" in args
        if end < start or end - start + 1 > _MAX_PAGE_LINES:
            return ToolResult.error(
                f"行范围无效：需满足 start_line <= end_line，且单次不超过 {_MAX_PAGE_LINES} 行",
                code="invalid_arguments",
                retryable=True,
                executed=False,
            )

        selected: list[str] = []
        selected_chars = 0
        char_truncated = False
        total_lines = 0
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                for total_lines, line in enumerate(handle, start=1):
                    if total_lines < start or total_lines > end or char_truncated:
                        continue
                    remaining = _MAX_READ_CHARS - selected_chars
                    if len(line) <= remaining:
                        selected.append(line)
                        selected_chars += len(line)
                    else:
                        selected.append(_truncate_long_line(line, max(remaining, 0)))
                        selected_chars = _MAX_READ_CHARS
                        char_truncated = True
        except UnicodeDecodeError:
            return ToolResult.error(
                f"无法以 UTF-8 读取（可能是二进制文件）：{path}", code="decode_error"
            )
        except OSError as exc:
            return ToolResult.error(f"读取失败：{exc}", code="io_error", retryable=True)

        if (total_lines or explicit_range) and start > total_lines:
            return ToolResult.error(
                f"start_line={start} 超出文件总行数 {total_lines}",
                code="range_out_of_bounds",
                retryable=True,
            )
        actual_end = min(end, total_lines)
        has_more = char_truncated or actual_end < total_lines
        content = "".join(selected)
        if not explicit_range and not has_more:
            output = content
        else:
            header = (
                f"[lines {start}-{actual_end} of {total_lines}, has_more={str(has_more).lower()}]"
            )
            parts = [header, content]
            if char_truncated:
                parts.append(f"[当前页超过 {_MAX_READ_CHARS} 字符，超长内容已截断]")
            if actual_end < total_lines:
                next_end = min(actual_end + _DEFAULT_PAGE_LINES, total_lines)
                parts.append(
                    f"[next: read_file(path={str(path)!r}, start_line={actual_end + 1}, "
                    f"end_line={next_end})]"
                )
            output = "\n".join(part.rstrip("\r\n") for part in parts if part != "")
        return ToolResult.ok(
            output,
            metadata={
                "path": str(path),
                "start_line": start,
                "end_line": actual_end,
                "total_lines": total_lines,
                "has_more": has_more,
                "char_truncated": char_truncated,
            },
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return path_request(
            self.name,
            Capability.FILESYSTEM_READ,
            args.get("path"),
            "读取文件内容",
            ctx,
        )


class ListDirTool(Tool):
    name = "list_dir"
    description = "列出目录下的文件和子目录；可用 max_results 限制大目录输出。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认当前目录"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_DIR_RESULTS,
                    "description": f"最多返回条目数，默认 {_DEFAULT_DIR_RESULTS}",
                },
            },
            "required": [],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve_path(args.get("path") or ".")
        except WorkspaceError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        if not path.exists():
            return ToolResult.error(f"目录不存在：{path}", code="not_found", retryable=True)
        if not path.is_dir():
            return ToolResult.error(f"不是目录：{path}", code="not_directory", retryable=True)
        limit = int(args.get("max_results", _DEFAULT_DIR_RESULTS))
        try:
            entries = heapq.nsmallest(
                limit + 1,
                path.iterdir(),
                key=lambda item: (item.is_file(), item.name.casefold(), item.name),
            )
        except OSError as exc:
            return ToolResult.error(f"列目录失败：{exc}", code="io_error", retryable=True)
        if not entries:
            return ToolResult.ok(f"{path} 为空目录", metadata={"returned": 0, "truncated": False})
        truncated = len(entries) > limit
        entries = entries[:limit]
        lines = [f"{'[dir] ' if entry.is_dir() else '[file]'} {entry.name}" for entry in entries]
        if truncated:
            lines.append(f"[已截断，仅显示前 {limit} 项；可缩小目录或提高 max_results]")
        return ToolResult.ok(
            "\n".join(lines), metadata={"returned": len(entries), "truncated": truncated}
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return path_request(
            self.name,
            Capability.FILESYSTEM_READ,
            args.get("path") or ".",
            "列出目录内容",
            ctx,
        )


def _truncate_long_line(line: str, limit: int) -> str:
    if limit <= 0:
        return ""
    marker = "\n[…当前行过长，已省略中间内容…]\n"
    if len(line) <= limit:
        return line
    if limit <= len(marker):
        return marker[:limit]
    keep = limit - len(marker)
    head = keep // 2
    return line[:head] + marker + line[-(keep - head) :]
