"""单机 Session 执行租约；业务状态仍只保存在 Run checkpoint。"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import BinaryIO

from assistant_agent.contracts.errors import RunStillActiveError

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PROCESS_LOCK = threading.Lock()
_HELD_PATHS: set[Path] = set()


class FileSessionExecutionLease:
    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self._path = path
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            with _PROCESS_LOCK:
                _HELD_PATHS.discard(self._path)

    def __enter__(self) -> FileSessionExecutionLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class FileSessionExecutionLeaseManager:
    """使用非阻塞 OS 文件锁保证单机同一 Session 只有一个执行者。"""

    def __init__(self, base_dir: str | Path) -> None:
        self._dir = Path(base_dir).resolve()

    def acquire(self, session_id: str) -> FileSessionExecutionLease:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("非法 Session ID")
        self._dir.mkdir(parents=True, exist_ok=True)
        path = (self._dir / f"{session_id}.lock").resolve()
        if path.parent != self._dir:
            raise ValueError("Session lease 路径超出存储目录")
        with _PROCESS_LOCK:
            if path in _HELD_PATHS:
                raise RunStillActiveError("Session 当前仍有活跃执行器")
            _HELD_PATHS.add(path)
        handle: BinaryIO | None = None
        try:
            handle = path.open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            _lock(handle)
            return FileSessionExecutionLease(path, handle)
        except BaseException as exc:
            if handle is not None:
                handle.close()
            with _PROCESS_LOCK:
                _HELD_PATHS.discard(path)
            if isinstance(exc, RunStillActiveError):
                raise
            raise RunStillActiveError("Session 当前仍有活跃执行器") from exc


def _lock(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(  # type: ignore[attr-defined]
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )
    except OSError as exc:
        raise RunStillActiveError("Session 当前仍有活跃执行器") from exc


def _unlock(handle: BinaryIO) -> None:
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
