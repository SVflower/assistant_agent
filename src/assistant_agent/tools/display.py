"""UI 无关的工具活动展示契约。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from assistant_agent.obs.redaction import redact_text, sanitize_for_display, truncate_text
from assistant_agent.tools.result import ToolResult

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b(?:\[[0-?]*[ -/]*[@-~])?")
_ACTIONS = {
    "read_file": "读取",
    "write_file": "写入",
    "edit_file": "编辑",
    "multi_edit": "批量编辑",
    "list_dir": "查看目录",
    "code_search": "搜索代码",
    "run_shell": "运行命令",
    "git": "检查 Git",
    "ask_user": "询问用户",
    "load_skill": "加载技能",
}


@dataclass(frozen=True)
class ToolDisplay:
    action: str
    target: str = ""
    summary: str = ""
    detail: str = ""


def safe_text(value: Any, limit: int = 500, *, multiline: bool = False) -> str:
    """脱敏并去除终端控制序列；单行模式同时转义换行。"""
    text = redact_text(str(value))
    text = _CONTROL_RE.sub("", text)
    if not multiline:
        text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return truncate_text(text, limit)


def _target(name: str, args: dict[str, Any]) -> str:
    if name in {"read_file", "write_file", "edit_file", "multi_edit", "list_dir"}:
        raw = str(args.get("path") or ".")
        try:
            path = Path(raw)
            if path.is_absolute():
                try:
                    relative = path.resolve().relative_to(Path.cwd().resolve())
                    return relative.as_posix() or "."
                except (OSError, ValueError):
                    return path.name or str(path)
            return path.as_posix()
        except (OSError, ValueError):
            return safe_text(raw, 100)
    if name == "code_search":
        pattern = safe_text(args.get("pattern", ""), 80)
        return f"/{pattern}/" if pattern else ""
    if name == "run_shell":
        return safe_text(args.get("command", ""), 120)
    if name == "git":
        command = f"git {args.get('subcommand', '')} {args.get('args', '')}".strip()
        return safe_text(command, 120)
    if name == "ask_user":
        return safe_text(args.get("question", ""), 100)
    if name == "load_skill":
        return safe_text(args.get("name", ""), 80)
    if name.startswith("mcp__"):
        return name.removeprefix("mcp__").replace("__", "/")
    return safe_text(name, 100)


def call_display(name: str, args: dict[str, Any]) -> ToolDisplay:
    sanitized = sanitize_for_display(args, 160)
    try:
        detail = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        detail = str(sanitized)
    action = _ACTIONS.get(name, "调用工具")
    return ToolDisplay(action, _target(name, args), detail=safe_text(detail, 500))


def result_display(
    name: str, args: dict[str, Any], result: ToolResult, call: ToolDisplay | None = None
) -> ToolDisplay:
    display = call or call_display(name, args)
    metadata = result.metadata
    if result.is_error:
        summary = safe_text(result.output.splitlines()[0] if result.output else result.code, 180)
    elif name == "read_file":
        start = metadata.get("start_line", 1)
        end = metadata.get("end_line", metadata.get("total_lines", 0))
        summary = f"已读取 {max(int(end) - int(start) + 1, 0)} 行"
    elif name == "write_file":
        content = str(args.get("content", ""))
        chars = int(metadata.get("chars", len(content)))
        lines = len(content.splitlines())
        summary = f"已写入 {chars} 字符，{lines} 行"
    elif name in {"edit_file", "multi_edit"}:
        summary = f"已替换 {metadata.get('replacements', 0)} 处"
    elif name == "list_dir":
        suffix = "（结果已截断）" if metadata.get("truncated") else ""
        summary = f"发现 {metadata.get('returned', 0)} 项{suffix}"
    elif name == "code_search":
        suffix = "（结果已截断）" if metadata.get("truncated") else ""
        summary = f"找到 {metadata.get('matches', 0)} 处匹配{suffix}"
    elif name in {"run_shell", "git"}:
        stdout = int(metadata.get("stdout_bytes", 0))
        stderr = int(metadata.get("stderr_bytes", 0))
        returncode = metadata.get("returncode", "?")
        summary = f"退出码 {returncode}，输出 {stdout + stderr} bytes"
    elif name == "ask_user":
        summary = safe_text(result.output, 120)
    elif name == "load_skill":
        summary = "技能已加载"
    else:
        summary = "调用完成"
    detail = safe_text(result.output, 1000, multiline=True)
    return replace(display, summary=summary, detail=detail)
