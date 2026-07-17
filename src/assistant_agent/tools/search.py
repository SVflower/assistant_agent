"""跨平台、有界内存的代码正则检索。"""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assistant_agent.runtime import WorkspaceError
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest

_IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    ".assistant_agent",
}
_DEFAULT_MAX_RESULTS = 100
_MAX_RESULTS = 500
_MAX_CONTEXT_LINES = 10
_MAX_DISPLAY_LINE = 300


@dataclass
class _Block:
    path: str
    start: int
    target_end: int
    lines: list[tuple[int, str]] = field(default_factory=list)
    matches: set[int] = field(default_factory=set)


class CodeSearchTool(Tool):
    name = "code_search"
    description = (
        "在文本文件中按正则搜索，返回文件、行号和内容；可用 context_lines 返回匹配前后上下文。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索根目录，默认当前目录"},
                "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
                "ignore_case": {"type": "boolean", "description": "忽略大小写，默认 false"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS,
                    "description": f"最多匹配数，默认 {_DEFAULT_MAX_RESULTS}",
                },
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": _MAX_CONTEXT_LINES,
                    "description": "每个匹配前后的上下文行数，默认 0",
                },
            },
            "required": ["pattern"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult.error(
                "缺少参数 pattern", code="invalid_arguments", retryable=True, executed=False
            )
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult.error(
                f"正则表达式无效：{exc}", code="invalid_arguments", retryable=True
            )

        try:
            root = ctx.resolve_path(args.get("path") or ".")
        except WorkspaceError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        if not root.exists():
            return ToolResult.error(f"路径不存在：{root}", code="not_found", retryable=True)
        glob = args.get("glob")
        max_results = int(args.get("max_results", _DEFAULT_MAX_RESULTS))
        context = int(args.get("context_lines", 0))
        output_parts: list[str] = []
        match_count = 0
        scanned_files = 0
        truncated = False

        for file in _iter_files(root, glob):
            scanned_files += 1
            try:
                if context == 0:
                    rows, found, overflow = _search_rows(
                        file, root, regex, max_results - match_count
                    )
                else:
                    rows, found, overflow = _search_blocks(
                        file, root, regex, context, max_results - match_count
                    )
            except (OSError, UnicodeDecodeError):
                continue
            output_parts.extend(rows)
            match_count += found
            if overflow:
                truncated = True
                break

        if not output_parts:
            return ToolResult.ok(
                "未找到匹配",
                metadata={"matches": 0, "scanned_files": scanned_files, "truncated": False},
            )
        if truncated:
            output_parts.append(f"[已截断，仅显示前 {max_results} 条匹配]")
        return ToolResult.ok(
            "\n".join(output_parts),
            metadata={
                "matches": match_count,
                "scanned_files": scanned_files,
                "truncated": truncated,
                "context_lines": context,
            },
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        root = ctx.resolve_path(args.get("path") or ".")
        return [
            PermissionRequest(
                self.name, Capability.FILESYSTEM_READ, str(root), "递归读取并搜索目录内容"
            )
        ]


def _search_rows(
    file: Path, root: Path, regex: re.Pattern[str], remaining: int
) -> tuple[list[str], int, bool]:
    rows: list[str] = []
    found = 0
    display = _display_path(file, root)
    with file.open("r", encoding="utf-8", newline="") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not regex.search(line):
                continue
            if found >= remaining:
                return rows, found, True
            rows.append(f"{display}:{lineno}: {_display_line(line)}")
            found += 1
    return rows, found, False


def _search_blocks(
    file: Path,
    root: Path,
    regex: re.Pattern[str],
    context: int,
    remaining: int,
) -> tuple[list[str], int, bool]:
    blocks: list[_Block] = []
    previous: deque[tuple[int, str]] = deque(maxlen=context)
    current: _Block | None = None
    found = 0
    overflow = False
    display = _display_path(file, root)

    with file.open("r", encoding="utf-8", newline="") as handle:
        for lineno, line in enumerate(handle, start=1):
            matched = bool(regex.search(line))
            overlaps = (
                matched and current is not None and lineno - context <= current.target_end + 1
            )
            if current is not None and lineno > current.target_end and not overlaps:
                blocks.append(current)
                current = None

            if matched and found >= remaining:
                overflow = True
                break
            if matched:
                found += 1
                start = max(1, lineno - context)
                if current is None:
                    current = _Block(
                        path=display,
                        start=start,
                        target_end=lineno + context,
                        lines=list(previous),
                    )
                else:
                    current.target_end = max(current.target_end, lineno + context)
                current.matches.add(lineno)

            if current is not None:
                current.lines.append((lineno, line))
            previous.append((lineno, line))

    if current is not None:
        blocks.append(current)
    return [_format_block(block) for block in blocks], found, overflow


def _format_block(block: _Block) -> str:
    actual_end = block.lines[-1][0] if block.lines else block.start
    lines = [f"{block.path}:{block.start}-{actual_end}"]
    lines.extend(
        f"{'>' if lineno in block.matches else ' '} {lineno}: {_display_line(text)}"
        for lineno, text in block.lines
    )
    return "\n".join(lines)


def _iter_files(root: Path, glob: str | None) -> Iterator[Path]:
    if root.is_file():
        if glob is None or fnmatch.fnmatch(root.name, glob):
            yield root
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if glob is not None and not fnmatch.fnmatch(path.name, glob):
            continue
        yield path


def _display_path(file: Path, root: Path) -> str:
    if root.is_file():
        return root.name
    try:
        return str(file.relative_to(root))
    except ValueError:
        return str(file)


def _display_line(line: str) -> str:
    value = line.rstrip("\r\n")
    if len(value) <= _MAX_DISPLAY_LINE:
        return value
    keep = _MAX_DISPLAY_LINE - len("…")
    head = keep // 2
    return value[:head] + "…" + value[-(keep - head) :]
