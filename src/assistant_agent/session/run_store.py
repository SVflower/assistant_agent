"""Run checkpoint 的双槽原子 JSON 存储。"""

from __future__ import annotations

import builtins
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DEFAULT_RUN_DIR = Path(".assistant_agent") / "runs"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class LoadedRun:
    document: dict[str, Any]
    source: Literal["current", "previous"]
    warning: str = ""


@dataclass(frozen=True)
class RunMeta:
    id: str
    status: str
    phase: str
    session_id: str | None
    updated_at: str
    preview: str


class RunStore:
    """不依赖 agent 类型的版本化 JSON 文档存储。"""

    def __init__(self, base_dir: str | Path = DEFAULT_RUN_DIR) -> None:
        self._dir = Path(base_dir)

    def _path(self, run_id: str, *, previous: bool = False) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("非法 Run ID")
        root = self._dir.resolve()
        suffix = ".prev.json" if previous else ".json"
        path = (root / f"{run_id}{suffix}").resolve()
        if path.parent != root:
            raise ValueError("Run 路径超出存储目录")
        return path

    @staticmethod
    def _encode(run_id: str, document: dict[str, Any]) -> bytes:
        if not isinstance(document, dict):
            raise TypeError("Run checkpoint 必须是 JSON object")
        if document.get("run_id") != run_id:
            raise ValueError("Run checkpoint ID 与目标 ID 不一致")
        text = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        return text.encode("utf-8", errors="strict")

    def save(self, run_id: str, document: dict[str, Any]) -> None:
        payload = self._encode(run_id, document)
        self._dir.mkdir(parents=True, exist_ok=True)
        current = self._path(run_id)
        previous = self._path(run_id, previous=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{run_id}-", suffix=".tmp", dir=self._dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if current.is_file():
                try:
                    self._read(current, run_id)
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    current.unlink(missing_ok=True)
                else:
                    os.replace(current, previous)
            os.replace(temp_path, current)
            self._fsync_directory()
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read(path: Path, expected_id: str) -> dict[str, Any]:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Run checkpoint 根节点必须是 object")
        if document.get("run_id") != expected_id:
            raise ValueError("Run checkpoint ID 与文件名不一致")
        return document

    def load(self, run_id: str) -> LoadedRun:
        current = self._path(run_id)
        previous = self._path(run_id, previous=True)
        current_error: Exception | None = None
        if current.is_file():
            try:
                return LoadedRun(self._read(current, run_id), "current")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                current_error = exc
        if previous.is_file():
            try:
                document = self._read(previous, run_id)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"Run checkpoint 两个槽均损坏：{run_id}") from exc
            warning = "current checkpoint 损坏，已回退 previous" if current_error else ""
            return LoadedRun(document, "previous", warning)
        if current_error is not None:
            raise ValueError(f"Run checkpoint 损坏且没有 previous：{run_id}") from current_error
        raise FileNotFoundError(f"Run 不存在：{run_id}")

    def list(self) -> list[RunMeta]:
        if not self._dir.is_dir():
            return []
        metas: list[RunMeta] = []
        run_ids = {
            path.name.removesuffix(".prev.json").removesuffix(".json")
            for path in self._dir.glob("*.json")
        }
        for run_id in run_ids:
            try:
                document = self.load(run_id).document
                task = str(document.get("task") or "").strip().replace("\n", " ")
                metas.append(
                    RunMeta(
                        id=run_id,
                        status=str(document.get("status") or "unknown"),
                        phase=str(document.get("phase") or "unknown"),
                        session_id=document.get("session_id"),
                        updated_at=str(document.get("updated_at") or ""),
                        preview=task[:40] + ("…" if len(task) > 40 else ""),
                    )
                )
            except (FileNotFoundError, ValueError):
                continue
        metas.sort(key=lambda item: item.updated_at, reverse=True)
        return metas

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(self._dir, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def delete(self, run_id: str) -> bool:
        removed = False
        for path in (self._path(run_id), self._path(run_id, previous=True)):
            if path.is_file():
                path.unlink()
                removed = True
        return removed

    def prune(self, max_terminal_runs: int) -> builtins.list[str]:
        if max_terminal_runs < 0:
            raise ValueError("max_terminal_runs 不能为负数")
        terminal = [
            item
            for item in self.list()
            if item.status in {"completed", "failed"}
            and bool(self.load(item.id).document.get("session_synced"))
        ]
        removed: builtins.list[str] = []
        for item in terminal[max_terminal_runs:]:
            if self.delete(item.id):
                removed.append(item.id)
        return removed
