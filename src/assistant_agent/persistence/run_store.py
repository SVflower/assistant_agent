"""Run checkpoint 的双槽原子 JSON 存储。

current 是最新状态，previous 是上一个已验证状态。原子替换避免读到半个 JSON，双槽则让 current
损坏时仍有保守恢复点；它不承诺外部工具副作用 exactly once。
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from assistant_agent.application.models import RunMeta, is_public_run_status
from assistant_agent.contracts.errors import UnsupportedRunStateSchemaError
from assistant_agent.contracts.time import parse_utc_timestamp
from assistant_agent.persistence.session_lifecycle import RunLifecycle, SessionLifecycle

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_INDEX_VERSION = 1
_INDEX_LOCK_ID = "session-index-v1"
_VERIFIED_INDEX_EPOCHS_GUARD = threading.Lock()
_VERIFIED_INDEX_EPOCHS: dict[Path, tuple[str, int, int]] = {}


@dataclass(frozen=True)
class LoadedRun:
    document: dict[str, Any]
    source: Literal["current", "previous"]
    warning: str = ""


class _StaleSessionIndexError(Exception):
    pass


class RunStore:
    """双槽 Run 文档存储；删除后以持久 tombstone 禁止同 ID 再创建或保存。

    tombstone 防止迟到 worker 在删除后重新保存同一个 Run。Session 索引用于跨进程查询归属，
    不能只依赖进程内缓存。
    """

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
        self._index_lifecycle = RunLifecycle(self._dir / ".index-lifecycle")
        self._session_index = self._dir / ".session-index-v1"
        self._ensure_session_index()

    def _path(self, run_id: str, *, previous: bool = False) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("非法 Run ID")
        root = self._dir.resolve()
        suffix = ".prev.json" if previous else ".json"
        path = (root / f"{run_id}{suffix}").resolve()
        if path.parent != root:
            raise ValueError("Run 路径超出存储目录")
        return path

    def _session_index_dir(self, generation: str, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _RUN_ID.fullmatch(session_id):
            raise ValueError("非法 Session ID")
        if not isinstance(generation, str) or not _RUN_ID.fullmatch(generation):
            raise ValueError("非法 Run Session 索引 generation")
        root = (self._session_index / generation).resolve()
        path = (root / session_id).resolve()
        if path.parent != root:
            raise ValueError("Run Session 索引路径超出存储目录")
        return path

    def _session_ref_path(self, generation: str, session_id: str, run_id: str) -> Path:
        self._path(run_id)
        return self._session_index_dir(generation, session_id) / f"{run_id}.ref"

    @staticmethod
    def _atomic_write(target: Path, payload: bytes, *, prefix: str) -> None:
        # 临时文件必须和目标位于同一目录，os.replace 才能提供同一文件系统内的原子发布。
        # fsync 文件和目录项用于降低断电后“内容写了但名字没落盘”的风险。
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            RunStore._fsync_file(target)
            RunStore._fsync_directory_path(target.parent)
            RunStore._fsync_directory_path(target.parent.parent)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory_path(path: Path) -> None:
        """尽力刷目录项；Windows 不提供可移植的目录 fsync。"""
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

    def _write_session_ref(self, generation: str, session_id: str, run_id: str) -> None:
        target = self._session_ref_path(generation, session_id, run_id)
        payload = json.dumps(
            {"run_id": run_id, "session_id": session_id, "version": _INDEX_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if target.is_file():
            try:
                if target.read_bytes() == payload:
                    return
            except OSError:
                pass
        self._atomic_write(target, payload, prefix=f".{run_id}-")

    @property
    def _manifest_path(self) -> Path:
        return self._session_index / "manifest.json"

    def _write_manifest_locked(self, generation: str, sessions: dict[str, set[str]]) -> None:
        payload = json.dumps(
            {
                "generation": generation,
                "sessions": {
                    session_id: sorted(run_ids)
                    for session_id, run_ids in sorted(sessions.items())
                    if run_ids
                },
                "version": _INDEX_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        self._atomic_write(self._manifest_path, payload, prefix=".manifest-")

    def _read_manifest_locked(self) -> tuple[str, dict[str, set[str]]]:
        try:
            document = json.loads(self._manifest_path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Run Session 索引 manifest 损坏") from exc
        if not isinstance(document, dict) or set(document) != {
            "generation",
            "sessions",
            "version",
        }:
            raise ValueError("Run Session 索引 manifest 结构无效")
        generation = document["generation"]
        raw_sessions = document["sessions"]
        if (
            document["version"] != _INDEX_VERSION
            or not isinstance(generation, str)
            or not _RUN_ID.fullmatch(generation)
            or not isinstance(raw_sessions, dict)
        ):
            raise ValueError("Run Session 索引 manifest 字段无效")
        sessions: dict[str, set[str]] = {}
        seen_runs: set[str] = set()
        for session_id, raw_run_ids in raw_sessions.items():
            if (
                not isinstance(session_id, str)
                or not _RUN_ID.fullmatch(session_id)
                or not isinstance(raw_run_ids, list)
                or not raw_run_ids
            ):
                raise ValueError("Run Session 索引 manifest Session 无效")
            run_ids: set[str] = set()
            for run_id in raw_run_ids:
                if (
                    not isinstance(run_id, str)
                    or not _RUN_ID.fullmatch(run_id)
                    or run_id in run_ids
                    or run_id in seen_runs
                ):
                    raise ValueError("Run Session 索引 manifest Run 无效")
                run_ids.add(run_id)
                seen_runs.add(run_id)
            sessions[session_id] = run_ids
        return generation, sessions

    def _validate_refs_locked(
        self,
        generation: str,
        sessions: dict[str, set[str]],
        *,
        session_id: str | None = None,
    ) -> None:
        root = (self._session_index / generation).resolve()
        if not root.is_dir():
            raise ValueError("Run Session 索引 generation 缺失")
        expected_sessions = (
            sessions if session_id is None else {session_id: sessions.get(session_id, set())}
        )
        for expected_session, expected_run_ids in expected_sessions.items():
            index_dir = self._session_index_dir(generation, expected_session)
            if not expected_run_ids:
                if index_dir.exists():
                    raise ValueError("Run Session 索引包含未登记 ref")
                continue
            if not index_dir.is_dir():
                raise ValueError("Run Session 索引目录缺失")
            actual_paths = list(index_dir.glob("*.ref"))
            if {path.stem for path in actual_paths} != expected_run_ids:
                raise ValueError("Run Session 索引 ref 集合不完整")
            for path in actual_paths:
                try:
                    document = json.loads(path.read_text(encoding="ascii"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("Run Session 索引 ref 损坏") from exc
                if document != {
                    "run_id": path.stem,
                    "session_id": expected_session,
                    "version": _INDEX_VERSION,
                }:
                    raise ValueError("Run Session 索引 ref 内容无效")
        if session_id is None:
            actual_dirs = {path.name for path in root.iterdir() if path.is_dir()}
            if actual_dirs != set(sessions):
                raise ValueError("Run Session 索引目录集合不完整")

    def _cleanup_index_locked(self, *, keep_generation: str) -> None:
        if not self._session_index.is_dir():
            return
        for path in self._session_index.iterdir():
            if path.name in {"manifest.json", keep_generation}:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            elif path.name == ".ready" or path.suffix == ".tmp":
                path.unlink(missing_ok=True)
        generation_dir = self._session_index / keep_generation
        if generation_dir.is_dir():
            for temp_path in generation_dir.rglob("*.tmp"):
                temp_path.unlink(missing_ok=True)

    def _authoritative_sessions_locked(self) -> dict[str, set[str]]:
        sessions: dict[str, set[str]] = {}
        if self._dir.is_dir():
            run_ids = {
                path.name.removesuffix(".prev.json").removesuffix(".json")
                for path in self._dir.glob("*.json")
            }
            for run_id in run_ids:
                with self._run_lifecycle.lock(run_id):
                    if self._run_lifecycle.is_deleted_locked(run_id):
                        continue
                    try:
                        document = self._load_unchecked(run_id).document
                    except (FileNotFoundError, ValueError):
                        continue
                    session_id = document.get("session_id")
                    if isinstance(session_id, str) and _RUN_ID.fullmatch(session_id):
                        sessions.setdefault(session_id, set()).add(run_id)
        return sessions

    def _build_session_index_locked(
        self, sessions: dict[str, set[str]]
    ) -> tuple[str, dict[str, set[str]]]:
        generation = f"g-{uuid.uuid4().hex}"
        (self._session_index / generation).mkdir(parents=True, exist_ok=False)
        self._fsync_directory_path(self._session_index)
        for session_id, run_ids in sessions.items():
            for run_id in run_ids:
                self._write_session_ref(generation, session_id, run_id)
        self._write_manifest_locked(generation, sessions)
        self._cleanup_index_locked(keep_generation=generation)
        return generation, sessions

    def _rebuild_session_index_locked(self) -> tuple[str, dict[str, set[str]]]:
        generation, sessions = self._build_session_index_locked(
            self._authoritative_sessions_locked()
        )
        self._mark_index_epoch_verified_locked()
        return generation, sessions

    def _index_locked(self, *, session_id: str | None = None) -> tuple[str, dict[str, set[str]]]:
        try:
            generation, sessions = self._read_manifest_locked()
            self._validate_refs_locked(generation, sessions, session_id=session_id)
        except (OSError, ValueError):
            generation, sessions = self._rebuild_session_index_locked()
            self._validate_refs_locked(generation, sessions, session_id=session_id)
        return generation, sessions

    def _ensure_session_index(self) -> None:
        with self._index_lifecycle.lock(_INDEX_LOCK_ID):
            generation, sessions = self._index_locked()
            generation, sessions = self._verify_index_epoch_locked(generation, sessions)
            self._cleanup_index_locked(keep_generation=generation)

    def _verify_index_epoch_locked(
        self, generation: str, sessions: dict[str, set[str]]
    ) -> tuple[str, dict[str, set[str]]]:
        epoch = self._index_epoch_locked()
        with _VERIFIED_INDEX_EPOCHS_GUARD:
            already_verified = _VERIFIED_INDEX_EPOCHS.get(epoch[0]) == epoch[1:]
        if already_verified:
            return generation, sessions
        authoritative = self._authoritative_sessions_locked()
        if sessions != authoritative:
            generation, sessions = self._build_session_index_locked(authoritative)
        self._validate_refs_locked(generation, sessions)
        self._mark_index_epoch_verified_locked()
        return generation, sessions

    def _mark_index_epoch_verified_locked(self) -> None:
        epoch = self._index_epoch_locked()
        with _VERIFIED_INDEX_EPOCHS_GUARD:
            _VERIFIED_INDEX_EPOCHS[epoch[0]] = epoch[1:]

    def _index_epoch_locked(self) -> tuple[Path, str, int, int]:
        manifest_digest = hashlib.sha256(self._manifest_path.read_bytes()).hexdigest()
        try:
            stat = self._dir.stat()
        except FileNotFoundError:
            directory_mtime_ns = 0
            directory_size = 0
        else:
            directory_mtime_ns = stat.st_mtime_ns
            directory_size = stat.st_size
        return self._dir.resolve(), manifest_digest, directory_mtime_ns, directory_size

    @staticmethod
    def _encode(run_id: str, document: dict[str, Any]) -> bytes:
        if not isinstance(document, dict):
            raise TypeError("Run checkpoint 必须是 JSON object")
        if document.get("run_id") != run_id:
            raise ValueError("Run checkpoint ID 与目标 ID 不一致")
        RunStore._require_current_schema(document)
        text = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        return text.encode("utf-8", errors="strict")

    def save(self, run_id: str, document: dict[str, Any]) -> int:
        """首次保存创建 Run；后续保存轮转槽位；tombstone 后永久拒写同 ID。"""
        payload = self._encode(run_id, document)
        session_id = document.get("session_id")
        if session_id is None:
            self._save_with_run_lock(run_id, payload)
            return len(payload)
        if not isinstance(session_id, str):
            raise ValueError("Run checkpoint session_id 必须是字符串或 null")
        with self._lifecycle.lock(session_id):
            if self._lifecycle.is_deleted_locked(session_id):
                raise FileNotFoundError(f"Session 已删除：{session_id}")
            with self._index_lifecycle.lock(_INDEX_LOCK_ID):
                generation, sessions = self._index_locked(session_id=session_id)
                generation, sessions = self._verify_index_epoch_locked(generation, sessions)
                indexed_session = next(
                    (candidate for candidate, run_ids in sessions.items() if run_id in run_ids),
                    None,
                )
                if indexed_session not in {None, session_id}:
                    raise ValueError("Run checkpoint 不得更换 Session")
                sessions.setdefault(session_id, set()).add(run_id)
                self._write_session_ref(generation, session_id, run_id)
                self._write_manifest_locked(generation, sessions)
                self._save_with_run_lock(run_id, payload)
                self._mark_index_epoch_verified_locked()
        return len(payload)

    def _save_with_run_lock(self, run_id: str, payload: bytes) -> None:
        with self._run_lifecycle.lock(run_id):
            if self._run_lifecycle.is_deleted_locked(run_id):
                raise FileNotFoundError(f"Run 已删除：{run_id}")
            session_id = json.loads(payload).get("session_id")
            existing_session = self._existing_session_id_locked(run_id)
            if existing_session is not None and existing_session != session_id:
                raise ValueError("Run checkpoint 不得更换或移除 Session")
            self._save_unchecked(run_id, payload)

    def _existing_session_id_locked(self, run_id: str) -> str | None:
        try:
            session_id = self._load_unchecked(run_id).document.get("session_id")
        except FileNotFoundError:
            return None
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("Run checkpoint session_id 必须是字符串或 null")
        return session_id

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
            self._fsync_file(current)
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
        RunStore._require_current_schema(document)
        return document

    @staticmethod
    def _require_current_schema(document: dict[str, Any]) -> None:
        actual = document.get("schema_version")
        if actual != 11:
            raise UnsupportedRunStateSchemaError(
                "Run checkpoint schema 不兼容：需要 v11",
                expected_version=11,
                actual_version=actual,
            )

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

    def last_for_session_locked(self, session_id: str) -> RunMeta | None:
        """调用方已持 Session lifecycle 锁时，一次聚合该 Session 的最后 Run。"""
        if self._lifecycle.is_deleted_locked(session_id):
            raise FileNotFoundError(f"Session 已删除：{session_id}")
        with self._index_lifecycle.lock(_INDEX_LOCK_ID):
            generation, sessions = self._index_locked(session_id=session_id)
            generation, sessions = self._verify_index_epoch_locked(generation, sessions)
            run_ids = sessions.get(session_id, set())
            try:
                return self._last_indexed_run_locked(session_id, run_ids)
            except _StaleSessionIndexError:
                generation, sessions = self._rebuild_session_index_locked()
                self._validate_refs_locked(generation, sessions, session_id=session_id)
                return self._last_indexed_run_locked(session_id, sessions.get(session_id, set()))

    def _last_indexed_run_locked(self, session_id: str, run_ids: set[str]) -> RunMeta | None:
        last: RunMeta | None = None
        for run_id in run_ids:
            with self._run_lifecycle.lock(run_id):
                if self._run_lifecycle.is_deleted_locked(run_id):
                    raise _StaleSessionIndexError
                try:
                    document = self._load_unchecked(run_id).document
                except (FileNotFoundError, ValueError) as exc:
                    raise _StaleSessionIndexError from exc
                if document.get("session_id") != session_id:
                    raise _StaleSessionIndexError
                status = str(document.get("status") or "unknown")
                if not is_public_run_status(status):
                    continue
                task = str(document.get("task") or "").strip().replace("\n", " ")
                candidate = RunMeta(
                    id=run_id,
                    status=status,
                    phase=str(document.get("phase") or "unknown"),
                    session_id=session_id,
                    updated_at=str(document.get("updated_at") or ""),
                    preview=task[:40] + ("…" if len(task) > 40 else ""),
                )
                if last is None or (parse_utc_timestamp(candidate.updated_at), candidate.id) > (
                    parse_utc_timestamp(last.updated_at),
                    last.id,
                ):
                    last = candidate
        return last

    def delete_session_runs(self, session_id: str) -> builtins.list[str]:
        """在 tombstone 后删除该 Session 的所有 Run 槽位。"""
        removed: builtins.list[str] = []
        with self._lifecycle.lock(session_id):
            if not self._lifecycle.is_deleted_locked(session_id):
                raise ValueError("删除 Run 前必须先发布 Session tombstone")
            with self._index_lifecycle.lock(_INDEX_LOCK_ID):
                generation, sessions = self._rebuild_session_index_locked()
                run_ids = sessions.get(session_id, set()).copy()
                for run_id in sorted(run_ids):
                    with self._run_lifecycle.lock(run_id):
                        if self._delete_locked(run_id):
                            removed.append(run_id)
                self._remove_session_refs_locked(generation, sessions, session_id)
                self._mark_index_epoch_verified_locked()
        return removed

    def _remove_session_refs_locked(
        self, generation: str, sessions: dict[str, set[str]], session_id: str
    ) -> None:
        sessions.pop(session_id, None)
        shutil.rmtree(self._session_index_dir(generation, session_id), ignore_errors=True)
        self._write_manifest_locked(generation, sessions)

    def _fsync_directory(self) -> None:
        self._fsync_directory_path(self._dir)

    def delete(self, run_id: str) -> bool:
        """存在双槽时先发布 tombstone 再清理；不存在或已删除返回 False。"""
        with self._index_lifecycle.lock(_INDEX_LOCK_ID):
            generation, sessions = self._index_locked()
            generation, sessions = self._verify_index_epoch_locked(generation, sessions)
            session_id = next(
                (candidate for candidate, run_ids in sessions.items() if run_id in run_ids),
                None,
            )
            with self._run_lifecycle.lock(run_id):
                deleted = self._delete_locked(run_id)
            if session_id is not None:
                run_ids = sessions.get(session_id)
                if run_ids is not None:
                    run_ids.discard(run_id)
                    ref = self._session_ref_path(generation, session_id, run_id)
                    ref.unlink(missing_ok=True)
                    if not run_ids:
                        sessions.pop(session_id)
                        shutil.rmtree(ref.parent, ignore_errors=True)
                    self._write_manifest_locked(generation, sessions)
            self._mark_index_epoch_verified_locked()
            return deleted

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
