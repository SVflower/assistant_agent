"""文件操作工具：读、写、列目录。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant_agent.tools.base import Tool, ToolContext, ToolResult

# 单次读取的最大字符数，避免把超大文件灌进上下文
_MAX_READ_CHARS = 100_000


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
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.error(f"写入失败：{exc}")
        return ToolResult.ok(f"已写入 {path}（{len(content)} 字符）")


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
