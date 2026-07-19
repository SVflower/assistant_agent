"""Session 删除 tombstone 与跨进程短时 lifecycle 锁。"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LOCK_SHARDS = 64
_WINDOWS_LOCK_POLL_SECONDS = 0.05
_THREAD_LOCKS_GUARD = globals().get("_THREAD_LOCKS_GUARD", threading.Lock())
_THREAD_LOCKS: dict[Path, threading.RLock] = globals().get("_THREAD_LOCKS", {})
_THREAD_HELD_PATHS = globals().get("_THREAD_HELD_PATHS", threading.local())
_AT_FORK_ACQUIRED_LOCKS: list[threading.RLock] = globals().get("_AT_FORK_ACQUIRED_LOCKS", [])
_FORK_HOOKS_REGISTERED = globals().get("_FORK_HOOKS_REGISTERED", False)


def _is_windows_lock_contention(exc: OSError) -> bool:
    """Python msvcrt.locking 以无 winerror 的 EACCES 表示锁冲突。"""
    return exc.errno == errno.EACCES and getattr(exc, "winerror", None) is None


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not _is_windows_lock_contention(exc):
                    raise
                time.sleep(_WINDOWS_LOCK_POLL_SECONDS)
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


def _current_thread_held_paths() -> set[Path]:
    held_paths = getattr(_THREAD_HELD_PATHS, "paths", None)
    if held_paths is None:
        held_paths = set()
        _THREAD_HELD_PATHS.paths = held_paths
    return held_paths


def _fork_audit_hook(event: str, _args: tuple[object, ...]) -> None:
    if event == "os.fork" and _current_thread_held_paths():
        raise RuntimeError("当前线程持有 lifecycle 锁，禁止 fork")


def _before_fork() -> None:
    global _AT_FORK_ACQUIRED_LOCKS
    if _current_thread_held_paths():
        raise RuntimeError("当前线程持有 lifecycle 锁，禁止 fork")
    _THREAD_LOCKS_GUARD.acquire()
    acquired: list[threading.RLock] = []
    try:
        for path in sorted(_THREAD_LOCKS, key=str):
            lock = _THREAD_LOCKS[path]
            lock.acquire()
            acquired.append(lock)
    except BaseException:
        for lock in reversed(acquired):
            lock.release()
        _THREAD_LOCKS_GUARD.release()
        raise
    _AT_FORK_ACQUIRED_LOCKS = acquired


def _after_fork_parent() -> None:
    global _AT_FORK_ACQUIRED_LOCKS
    for lock in reversed(_AT_FORK_ACQUIRED_LOCKS):
        lock.release()
    _AT_FORK_ACQUIRED_LOCKS = []
    _THREAD_LOCKS_GUARD.release()


def _after_fork_child() -> None:
    global _AT_FORK_ACQUIRED_LOCKS
    global _THREAD_HELD_PATHS
    global _THREAD_LOCKS
    global _THREAD_LOCKS_GUARD
    _THREAD_LOCKS_GUARD = threading.Lock()
    _THREAD_LOCKS = {}
    _THREAD_HELD_PATHS = threading.local()
    _AT_FORK_ACQUIRED_LOCKS = []


def _register_fork_hooks() -> None:
    global _FORK_HOOKS_REGISTERED
    register_at_fork = getattr(os, "register_at_fork", None)
    if register_at_fork is None or _FORK_HOOKS_REGISTERED:
        return
    sys.addaudithook(_fork_audit_hook)
    register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )
    _FORK_HOOKS_REGISTERED = True


_register_fork_hooks()


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
            thread_lock = _THREAD_LOCKS.setdefault(path, threading.RLock())
        with thread_lock:
            held_paths = _current_thread_held_paths()
            if path in held_paths:
                yield
                return
            handle = path.open("a+b")
            acquired = False
            try:
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                _lock_file(handle)
                acquired = True
                held_paths.add(path)
                yield
            finally:
                held_paths.discard(path)
                if acquired:
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
