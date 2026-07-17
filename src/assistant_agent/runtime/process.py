"""跨平台进程树监管与有界双流捕获。"""

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

from assistant_agent.runtime.control import RunControl


class TerminationReason(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    PAUSED = "paused"
    CANCELLED = "cancelled"


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


class _Collector:
    def __init__(self, limit: int) -> None:
        self.limit = max(limit, 1)
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
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
        complete = self.total <= self.limit
        if complete:
            raw = bytes(self.head + self.tail)
        else:
            omitted = self.total - len(self.head) - len(self.tail)
            marker = f"\n[…省略 {omitted} bytes…]\n".encode()
            raw = bytes(self.head) + marker + bytes(self.tail)
        return CapturedStream(_decode(raw), self.total, complete)


@dataclass
class _ManagedProcess:
    process: subprocess.Popen[bytes]
    job: object | None = None

    def terminate_tree(self, *, force: bool, grace: float) -> None:
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            if not force:
                try:
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                    self.process.wait(timeout=grace)
                    return
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if self.job is not None:
                self.job.terminate()  # type: ignore[attr-defined]
            else:
                self.process.kill()
        else:
            sig = _sigkill() if force else signal.SIGTERM
            try:
                _killpg(self.process.pid, sig)
            except ProcessLookupError:
                return
            if not force:
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
    """拥有受管进程生命周期，支持 timeout、暂停、取消和退出兜底。"""

    def __init__(self, *, poll_interval: float = 0.05, terminate_grace: float = 1.0) -> None:
        self.poll_interval = poll_interval
        self.terminate_grace = terminate_grace
        self._active: dict[int, _ManagedProcess] = {}
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
        managed = _ManagedProcess(process)
        try:
            if os.name == "nt":
                from assistant_agent.runtime.process_windows import WindowsJob

                managed.job = WindowsJob(process)
        except BaseException:
            process.kill()
            process.wait()
            raise
        with self._lock:
            self._active[process.pid] = managed

        assert process.stdout is not None and process.stderr is not None
        stdout = _Collector(max_stream_chars)
        stderr = _Collector(max_stream_chars)
        threads = [
            threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        reason = TerminationReason.COMPLETED
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
            returncode = process.wait()
        finally:
            if process.poll() is None:
                managed.terminate_tree(force=True, grace=0)
                process.wait()
            for thread in threads:
                thread.join()
            with self._lock:
                self._active.pop(process.pid, None)
            managed.close()
        return BoundedProcessResult(returncode, stdout.finish(), stderr.finish(), reason)

    def close(self) -> None:
        with self._lock:
            active = list(self._active.values())
        for managed in active:
            managed.terminate_tree(force=True, grace=0)


def _drain(stream: IO[bytes], collector: _Collector) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            collector.add(chunk)
    finally:
        stream.close()


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
