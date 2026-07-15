"""代码检索工具（code_search）：纯 Python 实现的 grep。

为什么纯 Python 而不 shell 到系统 grep：Windows 没有 grep，shell 方案不跨平台。
只读、无副作用，因此不需要危险操作确认。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest

# 默认跳过的目录：体积大或与源码无关，搜进去既慢又是噪音。
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
# 单文件读取上限，避免把超大文件全读进来
_MAX_FILE_BYTES = 2_000_000


class CodeSearchTool(Tool):
    name = "code_search"
    description = (
        "在文件中按正则搜索内容（类似 grep），返回匹配的 文件:行号: 内容。"
        "用于理解代码库：定位函数/类定义、查找调用点、检索关键字。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索根目录，默认当前目录"},
                "glob": {
                    "type": "string",
                    "description": "文件名过滤，如 *.py；不填则搜所有文本文件",
                },
                "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
                "max_results": {
                    "type": "integer",
                    "description": "最多返回多少条匹配，默认 100",
                },
            },
            "required": ["pattern"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern")
        if not pattern:
            return ToolResult.error("缺少参数 pattern")
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult.error(f"正则表达式无效：{exc}")

        root = Path(args.get("path") or ".")
        if not root.exists():
            return ToolResult.error(f"路径不存在：{root}")
        glob = args.get("glob")
        max_results = int(args.get("max_results") or 100)

        matches: list[str] = []
        truncated = False
        for file in _iter_files(root, glob):
            if len(matches) >= max_results:
                truncated = True
                break
            try:
                if file.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # 跳过二进制/无法读取的文件
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = _display_path(file, root)
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= max_results:
                        truncated = True
                        break

        if not matches:
            return ToolResult.ok("未找到匹配")
        output = "\n".join(matches)
        if truncated:
            output += f"\n[已截断，仅显示前 {max_results} 条匹配]"
        return ToolResult.ok(output)

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        root = Path(args.get("path") or ".").expanduser().resolve()
        return [
            PermissionRequest(
                self.name, Capability.FILESYSTEM_READ, str(root), "递归读取并搜索目录内容"
            )
        ]


def _iter_files(root: Path, glob: str | None):
    """遍历 root 下的文件，跳过忽略目录，按 glob 过滤文件名。"""
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
    """尽量显示相对路径，失败则显示原路径。"""
    try:
        return str(file.relative_to(root))
    except ValueError:
        return str(file)
