"""Workspace 内受限文本 artifact 存储。"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from assistant_agent.tools.file_io import atomic_write_text
from assistant_agent.tools.result import ArtifactRef

_SAFE_PREFIX = re.compile(r"[^A-Za-z0-9_-]+")


class ArtifactStore:
    def __init__(self, workspace_root: Path, *, max_chars: int, max_files: int) -> None:
        self.workspace_root = workspace_root.resolve()
        self.root = (self.workspace_root / ".assistant_agent" / "artifacts").resolve()
        if self.root != self.workspace_root and self.workspace_root not in self.root.parents:
            raise ValueError("artifact 目录必须位于 workspace 内")
        self.max_chars = max(max_chars, 1)
        self.max_files = max(max_files, 1)

    def write_text(
        self, content: str, *, prefix: str = "tool-output", complete: bool = True
    ) -> ArtifactRef:
        self.root.mkdir(parents=True, exist_ok=True)
        self._prune(keep=self.max_files - 1)
        safe_prefix = _SAFE_PREFIX.sub("-", prefix).strip("-")[:40] or "tool-output"
        artifact_id = secrets.token_hex(8)
        target = (self.root / f"{safe_prefix}-{artifact_id}.txt").resolve()
        if self.root not in target.parents:
            raise ValueError("artifact 文件路径逃逸")
        stored = _bounded_text(content, self.max_chars)
        stored_complete = complete and len(stored) == len(content)
        atomic_write_text(target, stored)
        return ArtifactRef(
            id=artifact_id,
            path=target.relative_to(self.workspace_root).as_posix(),
            size_chars=len(stored),
            complete=stored_complete,
        )

    def _prune(self, *, keep: int) -> None:
        try:
            files = sorted(
                (path for path in self.root.glob("*.txt") if path.is_file()),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
                reverse=True,
            )
        except OSError:
            return
        for path in files[keep:]:
            try:
                path.unlink()
            except OSError:
                pass


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n[…artifact 超过硬上限，已省略中间内容…]\n"
    if limit <= len(marker):
        return marker[:limit]
    keep = limit - len(marker)
    head = keep // 2
    return value[:head] + marker + value[-(keep - head) :]
