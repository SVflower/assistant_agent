"""跨平台进程树监管与有界双流捕获。

命令 timeout 覆盖的不只是 `process.wait()`：还包括 stdout/stderr PIPE 排空与进程树清理。父进程退出但
后台子进程仍继承 PIPE 时，如果只等待 EOF，Agent 会看似永久卡住。
"""

from __future__ import annotations

import locale
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import IO

from assistant_agent.execution.control import RunControl


class TerminationReason(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    BACKGROUND_PROCESS = "background_process"


@dataclass(frozen=True)
class CapturedStream:
    text: str
    total_bytes: int
    complete: bool


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: CapturedStream
    stderr: CapturedStream
    termination_reason: TerminationReason = TerminationReason.COMPLETED
    execution_duration_ms: int = 0
    drain_duration_ms: int = 0
    cleanup_duration_ms: int = 0

    @property
    def complete(self) -> bool:
        return self.stdout.complete and self.stderr.complete

    @property
    def timed_out(self) -> bool:
        return self.termination_reason is TerminationReason.TIMEOUT

    @property
    def interrupted(self) -> bool:
        return self.termination_reason in {
            TerminationReason.PAUSED,
            TerminationReason.CANCELLED,
        }

    @property
    def background_process(self) -> bool:
        return self.termination_reason is TerminationReason.BACKGROUND_PROCESS


class BoundedCollector:
    """线程安全地保留输出头尾，并持续统计真实字节数。

    只保存前后片段可以限制内存，同时保留错误开头和末尾 traceback。来源端限制不能被 Registry 的
    字符限制替代，因为后者发生在子进程已经产生输出之后。
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(limit, 1)
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            self.total += len(chunk)
            remaining_head = self.head_limit - len(self.head)
            if remaining_head > 0:
                self.head.extend(chunk[:remaining_head])
                chunk = chunk[remaining_head:]
            if chunk and self.tail_limit > 0:
                self.tail.extend(chunk)
                if len(self.tail) > self.tail_limit:
                    del self.tail[: len(self.tail) - self.tail_limit]

    def finish(self) -> CapturedStream:
        with self._lock:
            complete = self.total <= self.limit
            if complete:
                raw = bytes(self.head + self.tail)
            else:
                omitted = self.total - len(self.head) - len(self.tail)
                marker = f"\n[…省略 {omitted} bytes…]\n".encode()
                raw = bytes(self.head) + marker + bytes(self.tail)
            total = self.total
        return CapturedStream(_decode(raw), total, complete)


@dataclass
class ManagedProcessHandle:
    process: subprocess.Popen[bytes]
    job: object | None = None

    def terminate_tree(self, *, force: bool, grace: float) -> None:
        if os.name == "nt":
            if not force and self.process.poll() is None:
                try:
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                    self.process.wait(timeout=grace)
                    return
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if self.job is not None:
                self.job.terminate()  # type: ignore[attr-defined]
            elif self.process.poll() is None:
                self.process.kill()
        else:
            sig = _sigkill() if force else signal.SIGTERM
            try:
                _killpg(self.process.pid, sig)
            except ProcessLookupError:
                return
            if not force and self.process.poll() is None:
                try:
                    self.process.wait(timeout=grace)
                    return
                except subprocess.TimeoutExpired:
                    try:
                        _killpg(self.process.pid, _sigkill())
                    except ProcessLookupError:
                        pass

    def close(self) -> None:
        if self.job is not None:
            self.job.close()  # type: ignore[attr-defined]


class ProcessSupervisor:
    """拥有受管进程生命周期，支持 timeout、暂停、取消和退出兜底。

    每个进程以 Windows Job Object 或 POSIX process group 管理，因此 timeout/Runtime.close 能清理
    整棵树，而不只杀掉直接子进程。
    """

    def __init__(
        self,
        *,
        poll_interval: float = 0.05,
        terminate_grace: float = 1.0,
        drain_grace: float = 0.25,
    ) -> None:
        self.poll_interval = poll_interval
        self.terminate_grace = terminate_grace
        self.drain_grace = drain_grace
        self._active: dict[int, ManagedProcessHandle] = {}
        self._lock = threading.Lock()

    def run(
        self,
        command: str | list[str],
        *,
        shell: bool,
        timeout: float,
        max_stream_chars: int,
        cwd: str | None = None,
        control: RunControl | None = None,
    ) -> BoundedProcessResult:
        started_at = time.monotonic()
        managed = spawn_managed_process(command, shell=shell, cwd=cwd)
        process = managed.process
        with self._lock:
            self._active[process.pid] = managed

        assert process.stdout is not None and process.stderr is not None
        stdout = BoundedCollector(max_stream_chars)
        stderr = BoundedCollector(max_stream_chars)
        # stdout 和 stderr 必须并行排空。顺序读取任一 PIPE 都可能因另一 PIPE 缓冲区写满而死锁。
        threads = [
            threading.Thread(target=drain_stream, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain_stream, args=(process.stderr, stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        reason = TerminationReason.COMPLETED
        returncode = -1
        execution_duration_ms = 0
        drain_duration_ms = 0
        cleanup_duration_ms = 0
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if control is not None and control.cancel_requested:
                    reason = TerminationReason.CANCELLED
                    managed.terminate_tree(force=True, grace=self.terminate_grace)
                    break
                if control is not None and control.pause_requested:
                    reason = TerminationReason.PAUSED
                    managed.terminate_tree(force=False, grace=self.terminate_grace)
                    if control.cancel_requested:
                        reason = TerminationReason.CANCELLED
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = TerminationReason.TIMEOUT
                    managed.terminate_tree(force=False, grace=self.terminate_grace)
                    break
                if control is None:
                    time.sleep(min(self.poll_interval, remaining))
                else:
                    control.wait(min(self.poll_interval, remaining))
            returncode = bounded_wait(process, self.terminate_grace)
            execution_duration_ms = int((time.monotonic() - started_at) * 1000)

            # A shell can exit after spawning a descendant that inherited our PIPE handles.
            # Readers would then wait forever for EOF unless the owned process tree is closed.
            drain_started_at = time.monotonic()
            drained = join_threads(threads, self.drain_grace)
            drain_duration_ms = int((time.monotonic() - drain_started_at) * 1000)
            if not drained:
                if reason is TerminationReason.COMPLETED:
                    reason = TerminationReason.BACKGROUND_PROCESS
                cleanup_started_at = time.monotonic()
                managed.terminate_tree(force=True, grace=0)
                managed.close()
                join_threads(threads, self.terminate_grace)
                cleanup_duration_ms += int((time.monotonic() - cleanup_started_at) * 1000)
        finally:
            cleanup_started_at = time.monotonic()
            if process.poll() is None:
                managed.terminate_tree(force=True, grace=0)
                returncode = bounded_wait(process, self.terminate_grace)
            managed.close()
            if not join_threads(threads, self.terminate_grace):
                close_stream(process.stdout)
                close_stream(process.stderr)
                join_threads(threads, self.poll_interval)
            with self._lock:
                self._active.pop(process.pid, None)
            cleanup_duration_ms += int((time.monotonic() - cleanup_started_at) * 1000)
        return BoundedProcessResult(
            returncode,
            stdout.finish(),
            stderr.finish(),
            reason,
            execution_duration_ms,
            drain_duration_ms,
            cleanup_duration_ms,
        )

    def close(self) -> None:
        with self._lock:
            active = list(self._active.values())
        for managed in active:
            managed.terminate_tree(force=True, grace=0)


def spawn_managed_process(
    command: str | list[str], *, shell: bool, cwd: str | None = None
) -> ManagedProcessHandle:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        shell=shell,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    managed = ManagedProcessHandle(process)
    try:
        if os.name == "nt":
            from assistant_agent.execution.process_windows import WindowsJob

            managed.job = WindowsJob(process)
    except BaseException:
        process.kill()
        process.wait()
        raise
    return managed


def drain_stream(stream: IO[bytes], collector: BoundedCollector) -> None:
    try:
        read = getattr(stream, "read1", stream.read)
        while chunk := read(64 * 1024):
            collector.add(chunk)
    except (OSError, ValueError):
        pass
    finally:
        close_stream(stream)


def bounded_wait(process: subprocess.Popen[bytes], timeout: float) -> int:
    try:
        return process.wait(timeout=max(timeout, 0.01))
    except subprocess.TimeoutExpired:
        return process.returncode if process.returncode is not None else -1


def join_threads(threads: list[threading.Thread], timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0)
    for thread in threads:
        thread.join(max(deadline - time.monotonic(), 0))
    return all(not thread.is_alive() for thread in threads)


def close_stream(stream: IO[bytes] | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    encodings = ["utf-8"]
    if sys.platform == "win32":
        encodings.append(locale.getpreferredencoding(False))
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(encodings[-1], errors="replace")


def _killpg(pid: int, sig: int) -> None:
    os.killpg(pid, sig)  # type: ignore[attr-defined]


def _sigkill() -> int:
    return int(getattr(signal, "SIGKILL", signal.SIGTERM))
