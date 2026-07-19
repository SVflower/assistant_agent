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

from assistant_agent.application.models import RunMeta
from assistant_agent.contracts.time import parse_utc_timestamp
from assistant_agent.persistence.session_lifecycle import RunLifecycle, SessionLifecycle

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class LoadedRun:
    document: dict[str, Any]
    source: Literal["current", "previous"]
    warning: str = ""


class RunStore:
    """双槽 Run 文档存储；删除后以持久 tombstone 禁止同 ID 再创建或保存。"""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        lifecycle_dir: str | Path | None = None,
        run_lifecycle_dir: str | Path | None = None,
    ) -> None:
        if base_dir is None:
            from assistant_agent.config.paths import state_paths

            base_dir = state_paths().runs
        self._dir = Path(base_dir)
        self._lifecycle = SessionLifecycle(lifecycle_dir or self._dir.parent / "session-lifecycle")
        self._run_lifecycle = RunLifecycle(run_lifecycle_dir or self._dir / ".lifecycle")

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
        """首次保存创建 Run；后续保存轮转槽位；tombstone 后永久拒写同 ID。"""
        payload = self._encode(run_id, document)
        session_id = document.get("session_id")
        if session_id is None:
            self._save_with_run_lock(run_id, payload)
            return
        if not isinstance(session_id, str):
            raise ValueError("Run checkpoint session_id 必须是字符串或 null")
        with self._lifecycle.lock(session_id):
            if self._lifecycle.is_deleted_locked(session_id):
                raise FileNotFoundError(f"Session 已删除：{session_id}")
            self._save_with_run_lock(run_id, payload)

    def _save_with_run_lock(self, run_id: str, payload: bytes) -> None:
        with self._run_lifecycle.lock(run_id):
            if self._run_lifecycle.is_deleted_locked(run_id):
                raise FileNotFoundError(f"Run 已删除：{run_id}")
            self._save_unchecked(run_id, payload)

    def _save_unchecked(self, run_id: str, payload: bytes) -> None:
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

    def _load_unchecked(self, run_id: str) -> LoadedRun:
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

    def load(self, run_id: str) -> LoadedRun:
        """读取 live Run；已 tombstone 的 Run 即使残留槽位也按不存在处理。"""
        with self._run_lifecycle.lock(run_id):
            if self._run_lifecycle.is_deleted_locked(run_id):
                raise FileNotFoundError(f"Run 不存在：{run_id}")
            loaded = self._load_unchecked(run_id)
        session_id = loaded.document.get("session_id")
        if isinstance(session_id, str) and self._lifecycle.is_deleted(session_id):
            raise FileNotFoundError(f"Run 不存在：{run_id}")
        return loaded

    def list(self) -> list[RunMeta]:
        """仅列出可 load 的 live Run，并按规范 UTC instant 排序。"""
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
        metas.sort(
            key=lambda item: (parse_utc_timestamp(item.updated_at), item.id),
            reverse=True,
        )
        return metas

    def delete_session_runs(self, session_id: str) -> builtins.list[str]:
        """在 tombstone 后删除该 Session 的所有 Run 槽位。"""
        removed: builtins.list[str] = []
        with self._lifecycle.lock(session_id):
            if not self._lifecycle.is_deleted_locked(session_id):
                raise ValueError("删除 Run 前必须先发布 Session tombstone")
            if not self._dir.is_dir():
                return removed
            run_ids = {
                path.name.removesuffix(".prev.json").removesuffix(".json")
                for path in self._dir.glob("*.json")
            }
            for run_id in run_ids:
                with self._run_lifecycle.lock(run_id):
                    try:
                        loaded = self._load_unchecked(run_id)
                    except (FileNotFoundError, ValueError):
                        continue
                    if loaded.document.get("session_id") == session_id:
                        if self._delete_locked(run_id):
                            removed.append(run_id)
        return removed

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
        """存在双槽时先发布 tombstone 再清理；不存在或已删除返回 False。"""
        with self._run_lifecycle.lock(run_id):
            return self._delete_locked(run_id)

    def _delete_locked(self, run_id: str) -> bool:
        paths = (self._path(run_id), self._path(run_id, previous=True))
        if self._run_lifecycle.is_deleted_locked(run_id):
            for path in paths:
                path.unlink(missing_ok=True)
            return False
        if not any(path.is_file() for path in paths):
            return False
        self._run_lifecycle.mark_deleted_locked(run_id)
        for path in paths:
            path.unlink(missing_ok=True)
        return True

    def prune(self, max_terminal_runs: int) -> builtins.list[str]:
        if max_terminal_runs < 0:
            raise ValueError("max_terminal_runs 不能为负数")
        terminal = [
            item
            for item in self.list()
            if item.status in {"cancelled", "completed", "failed"}
            and bool(self.load(item.id).document.get("session_synced"))
        ]
        removed: builtins.list[str] = []
        for item in terminal[max_terminal_runs:]:
            if self.delete(item.id):
                removed.append(item.id)
        return removed
