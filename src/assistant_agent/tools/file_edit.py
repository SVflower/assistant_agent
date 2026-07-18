"""原子文本写入、精确编辑与多处编辑工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant_agent.execution import WorkspaceError
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.file_io import (
    adapt_newlines,
    atomic_write_text,
    dominant_newline,
    path_request,
    read_text_exact,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.tool import Tool


class _EditError(Exception):
    def __init__(self, message: str, code: str = "invalid_arguments") -> None:
        super().__init__(message)
        self.code = code


def _apply_one(content: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    if not old:
        raise _EditError("old_string 不能为空")
    newline = dominant_newline(content)
    adapted_old = adapt_newlines(old, newline)
    adapted_new = adapt_newlines(new, newline)
    count = content.count(adapted_old)
    if count == 0:
        raise _EditError(f"未找到要替换的文本：{old[:50]!r}", "no_match")
    if count > 1 and not replace_all:
        raise _EditError(
            f"要替换的文本出现 {count} 次，有歧义；请提供更精确的上下文，或设 replace_all=true",
            "ambiguous_edit",
        )
    return content.replace(adapted_old, adapted_new), (count if replace_all else 1)


def _read_for_edit(path: Path) -> str:
    if not path.exists():
        raise _EditError(f"文件不存在：{path}（新建请用 write_file）", "not_found")
    if not path.is_file():
        raise _EditError(f"不是文件：{path}", "not_file")
    try:
        return read_text_exact(path)
    except UnicodeDecodeError as exc:
        raise _EditError(f"无法以 UTF-8 读取（可能是二进制文件）：{path}", "decode_error") from exc


class WriteFileTool(Tool):
    name = "write_file"
    description = "原子地把完整内容写入文件（覆盖已有内容）；父目录不存在时自动创建。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "description": "目标文件路径"},
                "content": {"type": "string", "description": "完整文本内容"},
            },
            "required": ["path", "content"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve_path(args["path"])
        except WorkspaceError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        content = args["content"]
        try:
            atomic_write_text(path, content)
        except OSError as exc:
            return ToolResult.error(f"写入失败：{exc}", code="io_error", retryable=True)
        return ToolResult.ok(
            f"已原子写入 {path}（{len(content)} 字符）",
            metadata={"path": str(path), "chars": len(content), "atomic": True},
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return path_request(
            self.name,
            Capability.FILESYSTEM_WRITE,
            args.get("path"),
            "覆盖或创建文件",
            ctx,
        )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "原子地精确替换已存在文件中的一段文本，其余不动。old_string 须唯一，"
        "除非 replace_all=true；保持原文件换行风格。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "old_string": {"type": "string", "minLength": 1},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve_path(args["path"])
        except WorkspaceError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        try:
            content = _read_for_edit(path)
            new_content, count = _apply_one(
                content, args["old_string"], args["new_string"], bool(args.get("replace_all"))
            )
            atomic_write_text(path, new_content)
        except _EditError as exc:
            return ToolResult.error(str(exc), code=exc.code, retryable=True)
        except OSError as exc:
            return ToolResult.error(f"写入失败：{exc}", code="io_error", retryable=True)
        return ToolResult.ok(
            f"已原子编辑 {path}：替换 {count} 处",
            metadata={"path": str(path), "replacements": count, "atomic": True},
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return path_request(
            self.name,
            Capability.FILESYSTEM_WRITE,
            args.get("path"),
            "编辑文件内容",
            ctx,
        )


class MultiEditTool(Tool):
    name = "multi_edit"
    description = "对同一文件按顺序应用多处精确替换，再原子写入；任一处失败则文件完全不改。"

    @property
    def parameters(self) -> dict[str, Any]:
        edit_schema = {
            "type": "object",
            "properties": {
                "old_string": {"type": "string", "minLength": 1},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["old_string", "new_string"],
        }
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "edits": {"type": "array", "minItems": 1, "items": edit_schema},
            },
            "required": ["path", "edits"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve_path(args["path"])
        except WorkspaceError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        edits = args["edits"]
        try:
            content = _read_for_edit(path)
            total = 0
            for edit in edits:
                content, count = _apply_one(
                    content,
                    edit["old_string"],
                    edit["new_string"],
                    bool(edit.get("replace_all")),
                )
                total += count
            atomic_write_text(path, content)
        except _EditError as exc:
            return ToolResult.error(
                f"多处编辑中止（未改动文件）：{exc}", code=exc.code, retryable=True
            )
        except OSError as exc:
            return ToolResult.error(f"写入失败：{exc}", code="io_error", retryable=True)
        return ToolResult.ok(
            f"已原子编辑 {path}：{len(edits)} 处替换，共 {total} 处生效",
            metadata={
                "path": str(path),
                "edits": len(edits),
                "replacements": total,
                "atomic": True,
            },
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return path_request(
            self.name,
            Capability.FILESYSTEM_WRITE,
            args.get("path"),
            "批量编辑文件内容",
            ctx,
        )
