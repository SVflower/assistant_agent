"""版本化 Session JSON 存储、迁移与跨进程文档锁。"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from assistant_agent.application.models import (
    EMPTY_SESSION_TITLE,
    SESSION_SCHEMA_VERSION,
    Session,
    SessionMeta,
    automatic_session_title,
    public_message_count,
)

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}


class UnsupportedSessionSchemaError(ValueError):
    """Session 文档来自未知未来版本，调用方必须 fail closed。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_session_id() -> str:
    """时间戳 + 短随机后缀：可读、可排序、不撞。"""
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def _valid_title(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 100
        and any(not char.isspace() for char in value)
    )


def _migrate_document(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    version = data.get("schema_version")
    if version is None:
        migrated = dict(data)
        source = migrated.get("title_source")
        title = migrated.get("title")
        if not (_valid_title(title) and source in {"auto", "user"}):
            title = automatic_session_title(migrated.get("messages", []))
            source = "auto"
        migrated.update(
            schema_version=SESSION_SCHEMA_VERSION,
            title=title,
            title_source=source,
            metadata_version=1,
        )
        return migrated, True
    if type(version) is not int or version != SESSION_SCHEMA_VERSION:
        raise UnsupportedSessionSchemaError(f"不支持的 Session schema_version：{version!r}")
    if not _valid_title(data.get("title")):
        raise ValueError("Session title 不合法")
    if data.get("title_source") not in {"auto", "user"}:
        raise ValueError("Session title_source 不合法")
    metadata_version = data.get("metadata_version")
    if type(metadata_version) is not int or metadata_version < 1:
        raise ValueError("Session metadata_version 不合法")
    return data, False


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]


def _unlock_file(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:
        pass


class SessionStore:
    """会话文件的存取；每次文档读改写都持有短时跨进程锁。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            from assistant_agent.config.paths import state_paths

            base_dir = state_paths().sessions
        self._dir = Path(base_dir)

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("非法会话 ID")
        root = self._dir.resolve()
        path = (root / f"{session_id}.json").resolve()
        if path.parent != root:
            raise ValueError("会话路径超出存储目录")
        return path

    def _lock_path(self, session_id: str) -> Path:
        return self._path(session_id).with_suffix(".lock")

    @contextmanager
    def _document_lock(self, session_id: str) -> Iterator[None]:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_path(session_id)
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(path, threading.Lock())
        with thread_lock:
            handle = path.open("a+b")
            try:
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                _lock_file(handle)
                yield
            finally:
                _unlock_file(handle)
                handle.close()

    def new_session(self, provider: str = "", model: str = "") -> Session:
        now = _now_iso()
        return Session(
            id=new_session_id(),
            created_at=now,
            updated_at=now,
            provider=provider,
            model=model,
        )

    def _read_locked(self, session_id: str) -> tuple[Session, bool]:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"会话不存在：{session_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Session 文档根节点必须是 object")
        data, migrated = _migrate_document(raw)
        session = Session.from_dict(data)
        if session.id != session_id:
            raise ValueError("会话文件 ID 与请求 ID 不一致")
        return session, migrated

    def _atomic_write_locked(self, session: Session) -> None:
        text = json.dumps(session.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
        payload = text.encode("utf-8", errors="replace")
        target = self._path(session.id)
        fd, temp_name = tempfile.mkstemp(prefix=f".{session.id}-", suffix=".tmp", dir=self._dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            self._fsync_directory()
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def save(self, session: Session, messages: list[dict[str, Any]] | None = None) -> None:
        """锁内 fresh load，并把内容变化合并到最新元数据后原子替换。"""
        with self._document_lock(session.id):
            try:
                fresh, _ = self._read_locked(session.id)
            except FileNotFoundError:
                fresh = None
            if messages is not None:
                session.messages = messages
            if fresh is not None:
                session.title = fresh.title
                session.title_source = fresh.title_source
                session.metadata_version = fresh.metadata_version
                session.schema_version = fresh.schema_version
                session.created_at = fresh.created_at
            generated = automatic_session_title(session.messages)
            if (
                session.title_source == "auto"
                and session.title == EMPTY_SESSION_TITLE
                and generated != EMPTY_SESSION_TITLE
            ):
                session.title = generated
                session.metadata_version += 1
            session.updated_at = _now_iso()
            self._atomic_write_locked(session)

    def load(self, session_id: str) -> Session:
        """载入并在锁内幂等迁移旧 Session；未知未来版本拒绝读取。"""
        with self._document_lock(session_id):
            session, migrated = self._read_locked(session_id)
            if migrated:
                self._atomic_write_locked(session)
            return session

    def update_metadata(self, session_id: str, title: str, expected_version: int) -> Session:
        if not _valid_title(title):
            raise ValueError("Session title 不合法")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_metadata_version 不合法")
        with self._document_lock(session_id):
            session, migrated = self._read_locked(session_id)
            if migrated:
                self._atomic_write_locked(session)
            if session.metadata_version != expected_version:
                from assistant_agent.contracts.errors import SessionMetadataConflictError

                raise SessionMetadataConflictError(
                    "Session 元数据版本冲突",
                    current_metadata_version=session.metadata_version,
                )
            session.title = title
            session.title_source = "user"
            session.metadata_version += 1
            session.updated_at = _now_iso()
            self._atomic_write_locked(session)
            return session

    def list(self) -> list[SessionMeta]:
        if not self._dir.is_dir():
            return []
        metas: list[SessionMeta] = []
        for path in self._dir.glob("*.json"):
            try:
                session = self.load(path.stem)
            except UnsupportedSessionSchemaError:
                raise
            except (json.JSONDecodeError, KeyError, OSError, UnicodeError, ValueError):
                continue
            metas.append(
                SessionMeta(
                    id=session.id,
                    title=session.title,
                    title_source=session.title_source,
                    metadata_version=session.metadata_version,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    message_count=public_message_count(session.messages),
                    preview=session.preview,
                )
            )
        metas.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return metas

    def delete(self, session_id: str) -> bool:
        with self._document_lock(session_id):
            path = self._path(session_id)
            if not path.is_file():
                return False
            path.unlink()
            return True

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
