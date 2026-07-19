"""Session 删除 tombstone 与跨进程短时 lifecycle 锁。"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LOCK_SHARDS = 64
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}


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


class PersistentLifecycle:
    """为一个受约束 ID 提供跨进程锁与持久 tombstone。"""

    def __init__(self, base_dir: str | Path, *, entity: str) -> None:
        self._dir = Path(base_dir).resolve()
        self._entity = entity

    def _path(self, session_id: str, suffix: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError(f"非法 {self._entity} ID")
        name = session_id
        if suffix == "lock":
            shard = (
                int.from_bytes(hashlib.sha256(session_id.encode("ascii")).digest()[:2], "big")
                % _LOCK_SHARDS
            )
            name = f"lock-{shard:02d}"
        path = (self._dir / f"{name}.{suffix}").resolve()
        if path.parent != self._dir:
            raise ValueError(f"{self._entity} lifecycle 路径超出存储目录")
        return path

    @contextmanager
    def lock(self, session_id: str) -> Iterator[None]:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id, "lock")
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

    def is_deleted_locked(self, session_id: str) -> bool:
        return self._path(session_id, "deleted").is_file()

    def is_deleted(self, session_id: str) -> bool:
        with self.lock(session_id):
            return self.is_deleted_locked(session_id)

    def mark_deleted_locked(self, session_id: str) -> None:
        target = self._path(session_id, "deleted")
        if target.is_file():
            return
        fd, temp_name = tempfile.mkstemp(prefix=f".{session_id}-", suffix=".tmp", dir=self._dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"deleted\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise


class SessionLifecycle(PersistentLifecycle):
    """所有 Session 更新先短时取得该锁，再检查持久 tombstone。"""

    def __init__(self, base_dir: str | Path) -> None:
        super().__init__(base_dir, entity="Session")


class RunLifecycle(PersistentLifecycle):
    """所有 Run 双槽更新先短时取得该锁，再检查持久 tombstone。"""

    def __init__(self, base_dir: str | Path) -> None:
        super().__init__(base_dir, entity="Run")
