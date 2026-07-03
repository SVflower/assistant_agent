"""文件操作工具：读、写、列目录。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant_agent.tools.base import Tool, ToolContext, ToolResult

# 单次读取的最大字符数，避免把超大文件灌进上下文
_MAX_READ_CHARS = 100_000


def _within_workspace(path: Path, workspace_root: Path) -> bool:
    """path 是否在工作区根目录树内（用于写入范围判断）。"""
    try:
        path.resolve().relative_to(Path(workspace_root).resolve())
        return True
    except ValueError:
        return False


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取文本文件的内容。返回文件全文（过大时会截断）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要读取的文件路径（相对或绝对）"}
            },
            "required": ["path"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = args.get("path")
        if not path_str:
            return ToolResult.error("缺少参数 path")
        path = Path(path_str)
        if not path.exists():
            return ToolResult.error(f"文件不存在：{path}")
        if not path.is_file():
            return ToolResult.error(f"不是文件：{path}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.error(f"无法以 UTF-8 读取（可能是二进制文件）：{path}")
        except OSError as exc:
            return ToolResult.error(f"读取失败：{exc}")
        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + f"\n\n[已截断，仅显示前 {_MAX_READ_CHARS} 字符]"
        return ToolResult.ok(text)


class WriteFileTool(Tool):
    name = "write_file"
    description = "把内容写入文件（覆盖已有内容）。父目录不存在时自动创建。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件路径"},
                "content": {"type": "string", "description": "要写入的完整内容"},
            },
            "required": ["path", "content"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = args.get("path")
        if not path_str:
            return ToolResult.error("缺少参数 path")
        content = args.get("content", "")
        path = Path(path_str)

        # 工作区范围：区内写直接放行；区外写需确认（防"自己动别处文件"）。
        # 区内的可见/回滚靠流式显示 + git，不逐个弹窗。
        if not _within_workspace(path, ctx.workspace_root):
            allowed = ctx.request_confirm(
                "write_outside_workspace",
                f"即将写入工作区外的文件：\n  {path.resolve()}",
            )
            if not allowed:
                return ToolResult.error(f"用户拒绝写入工作区外：{path}")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.error(f"写入失败：{exc}")
        return ToolResult.ok(f"已写入 {path}（{len(content)} 字符）")


class _EditError(Exception):
    """单次替换失败（未找到/歧义），用于在多编辑中原子中止。"""


def _apply_one(content: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    """在 content 中把 old 替换为 new。

    唯一匹配才替换（防误替）；replace_all=True 则替换所有。
    未找到或（多次且未开 replace_all）→ 抛 _EditError。返回 (新内容, 替换次数)。
    """
    if not old:
        raise _EditError("old_string 不能为空")
    count = content.count(old)
    if count == 0:
        raise _EditError(f"未找到要替换的文本：{old[:50]!r}")
    if count > 1 and not replace_all:
        raise _EditError(
            f"要替换的文本出现 {count} 次，有歧义；请提供更精确的上下文，或设 replace_all=true"
        )
    return content.replace(old, new), (count if replace_all else 1)


def _read_for_edit(path: Path) -> str:
    """读取待编辑文件（须存在、可 UTF-8 解码）。"""
    if not path.exists():
        raise _EditError(f"文件不存在：{path}（新建请用 write_file）")
    if not path.is_file():
        raise _EditError(f"不是文件：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _EditError(f"无法以 UTF-8 读取（可能是二进制文件）：{path}") from exc


def _confirm_if_outside(path: Path, ctx: ToolContext) -> bool:
    """编辑区外文件需确认（对齐 write_file 的工作区范围）。"""
    if _within_workspace(path, ctx.workspace_root):
        return True
    return ctx.request_confirm(
        "write_outside_workspace",
        f"即将编辑工作区外的文件：\n  {path.resolve()}",
    )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "精确替换已存在文件中的一段文本，其余不动。"
        "局部改动优先用它，而非 write_file 整篇重写（省 token、更安全）。"
        "old_string 须在文件中唯一出现，除非设 replace_all。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要编辑的文件路径（须已存在）"},
                "old_string": {"type": "string", "description": "被替换的原文（须唯一出现）"},
                "new_string": {"type": "string", "description": "替换成的新文本"},
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有出现，默认 false",
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = args.get("path")
        if not path_str:
            return ToolResult.error("缺少参数 path")
        if "old_string" not in args or "new_string" not in args:
            return ToolResult.error("缺少参数 old_string 或 new_string")
        path = Path(path_str)
        if not _confirm_if_outside(path, ctx):
            return ToolResult.error(f"用户拒绝编辑工作区外：{path}")
        try:
            content = _read_for_edit(path)
            new_content, n = _apply_one(
                content, args["old_string"], args["new_string"], bool(args.get("replace_all"))
            )
            path.write_text(new_content, encoding="utf-8")
        except _EditError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"写入失败：{exc}")
        return ToolResult.ok(f"已编辑 {path}：替换 {n} 处")


class MultiEditTool(Tool):
    name = "multi_edit"
    description = (
        "对同一文件按顺序应用多处替换，原子写入（任一处失败则整体不改）。"
        "需要一次改多个地方时用它；每处规则同 edit_file（唯一匹配才替换）。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要编辑的文件路径（须已存在）"},
                "edits": {
                    "type": "array",
                    "description": "替换列表，按顺序应用",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["path", "edits"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = args.get("path")
        if not path_str:
            return ToolResult.error("缺少参数 path")
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return ToolResult.error("缺少参数 edits（需至少一处替换）")
        path = Path(path_str)
        if not _confirm_if_outside(path, ctx):
            return ToolResult.error(f"用户拒绝编辑工作区外：{path}")
        try:
            content = _read_for_edit(path)
            total = 0
            for i, edit in enumerate(edits, start=1):
                if not isinstance(edit, dict) or "old_string" not in edit:
                    raise _EditError(f"第 {i} 处替换缺少 old_string/new_string")
                content, n = _apply_one(
                    content,
                    edit["old_string"],
                    edit.get("new_string", ""),
                    bool(edit.get("replace_all")),
                )
                total += n
            path.write_text(content, encoding="utf-8")
        except _EditError as exc:
            return ToolResult.error(f"多处编辑中止（未改动文件）：{exc}")
        except OSError as exc:
            return ToolResult.error(f"写入失败：{exc}")
        return ToolResult.ok(f"已编辑 {path}：{len(edits)} 处替换，共 {total} 处生效")


class ListDirTool(Tool):
    name = "list_dir"
    description = "列出目录下的文件和子目录。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录路径，默认当前目录",
                }
            },
            "required": [],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = Path(args.get("path") or ".")
        if not path.exists():
            return ToolResult.error(f"目录不存在：{path}")
        if not path.is_dir():
            return ToolResult.error(f"不是目录：{path}")
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError as exc:
            return ToolResult.error(f"列目录失败：{exc}")
        if not entries:
            return ToolResult.ok(f"{path} 为空目录")
        lines = [f"{'[dir] ' if e.is_dir() else '[file]'} {e.name}" for e in entries]
        return ToolResult.ok("\n".join(lines))
