"""文件工具共享 I/O：权限目标、保换行读取与原子文本替换。"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.permissions import Capability, PermissionRequest


def path_request(
    tool: str,
    capability: Capability,
    path_value: Any,
    risk: str,
    ctx: ToolContext | None = None,
) -> list[PermissionRequest]:
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        return []
    target = str(
        ctx.resolve_path(path_value) if ctx is not None else Path(path_value).expanduser().resolve()
    )
    return [PermissionRequest(tool, capability, target, risk)]


def read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def atomic_write_text(path: Path, content: str) -> None:
    """同目录落临时文件，完整写盘后以 os.replace 作为提交点。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temp, existing_mode)
        os.replace(temp, path)
        replaced = True
        _fsync_directory(path.parent)
    finally:
        if not replaced:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def adapt_newlines(value: str, newline: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def dominant_newline(content: str) -> str:
    crlf = content.count("\r\n")
    bare_lf = content.count("\n") - crlf
    return "\r\n" if crlf > bare_lf else "\n"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
