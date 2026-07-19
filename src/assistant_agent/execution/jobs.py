"""Runtime-owned background process lifecycle."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from assistant_agent.execution.process import (
    BoundedCollector,
    CapturedStream,
    ManagedProcessHandle,
    bounded_wait,
    close_stream,
    drain_stream,
    join_threads,
    spawn_managed_process,
)

ManagedProcessStatus = Literal["running", "exited", "failed", "stopped"]


class ManagedProcessError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ManagedProcessSnapshot:
    process_id: str
    status: ManagedProcessStatus
    returncode: int | None
    stdout: CapturedStream
    stderr: CapturedStream
    elapsed_seconds: float
    error_code: str | None = None


@dataclass
class _BackgroundJob:
    process_id: str
    handle: ManagedProcessHandle
    stdout: BoundedCollector
    stderr: BoundedCollector
    readers: list[threading.Thread]
    started_at: float
    stopped: bool = False
    finalized: bool = False
    detached_child: bool = False


class ManagedProcessRegistry:
    """Owns background processes for exactly one Agent Runtime."""

    def __init__(
        self,
        *,
        max_processes: int = 4,
        max_stream_chars: int = 100_000,
        terminate_grace: float = 1.0,
        drain_grace: float = 0.25,
    ) -> None:
        self.max_processes = max(max_processes, 1)
        self.max_stream_chars = max(max_stream_chars, 1)
        self.terminate_grace = max(terminate_grace, 0.01)
        self.drain_grace = max(drain_grace, 0.01)
        self._jobs: dict[str, _BackgroundJob] = {}
        self._closed = False
        self._lock = threading.RLock()

    def start(self, command: str, *, cwd: str) -> ManagedProcessSnapshot:
        with self._lock:
            self._ensure_open()
            self._refresh_all()
            self._prune_terminal()
            active = sum(job.handle.process.poll() is None for job in self._jobs.values())
            if active >= self.max_processes:
                raise ManagedProcessError(
                    f"受管后台进程已达到上限（{self.max_processes}）。",
                    code="managed_process_limit",
                )
            handle = spawn_managed_process(command, shell=True, cwd=cwd)
            assert handle.process.stdout is not None and handle.process.stderr is not None
            stdout = BoundedCollector(self.max_stream_chars)
            stderr = BoundedCollector(self.max_stream_chars)
            readers = [
                threading.Thread(
                    target=drain_stream,
                    args=(handle.process.stdout, stdout),
                    daemon=True,
                    name="assistant-agent-process-stdout",
                ),
                threading.Thread(
                    target=drain_stream,
                    args=(handle.process.stderr, stderr),
                    daemon=True,
                    name="assistant-agent-process-stderr",
                ),
            ]
            process_id = f"proc-{uuid.uuid4().hex[:12]}"
            job = _BackgroundJob(process_id, handle, stdout, stderr, readers, time.monotonic())
            self._jobs[process_id] = job
            for reader in readers:
                reader.start()
            return self._snapshot(job)

    def get(self, process_id: str) -> ManagedProcessSnapshot:
        with self._lock:
            self._ensure_open()
            job = self._get_job(process_id)
            self._refresh(job)
            return self._snapshot(job)

    def list(self) -> list[ManagedProcessSnapshot]:
        with self._lock:
            self._ensure_open()
            self._refresh_all()
            return [self._snapshot(job) for job in self._jobs.values()]

    def stop(self, process_id: str) -> ManagedProcessSnapshot:
        with self._lock:
            self._ensure_open()
            job = self._get_job(process_id)
            self._refresh(job)
            if job.handle.process.poll() is None:
                job.stopped = True
                job.handle.terminate_tree(force=False, grace=self.terminate_grace)
                if job.handle.process.poll() is None:
                    job.handle.terminate_tree(force=True, grace=0)
                bounded_wait(job.handle.process, self.terminate_grace)
            self._finalize(job, clean_descendants=True)
            return self._snapshot(job)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                if job.handle.process.poll() is None:
                    job.stopped = True
                    job.handle.terminate_tree(force=True, grace=0)
                    bounded_wait(job.handle.process, self.terminate_grace)
                self._finalize(job, clean_descendants=True)

    def _refresh_all(self) -> None:
        for job in self._jobs.values():
            self._refresh(job)

    def _refresh(self, job: _BackgroundJob) -> None:
        if job.handle.process.poll() is not None and not job.finalized:
            if not join_threads(job.readers, self.drain_grace):
                job.detached_child = True
            self._finalize(job, clean_descendants=True)

    def _prune_terminal(self) -> None:
        retained = self.max_processes * 4
        terminal = [job for job in self._jobs.values() if job.finalized]
        for job in terminal[: max(len(self._jobs) - (retained - 1), 0)]:
            self._jobs.pop(job.process_id, None)

    def _finalize(self, job: _BackgroundJob, *, clean_descendants: bool) -> None:
        if job.finalized:
            return
        if clean_descendants:
            job.handle.terminate_tree(force=True, grace=0)
        job.handle.close()
        if not join_threads(job.readers, self.drain_grace):
            close_stream(job.handle.process.stdout)
            close_stream(job.handle.process.stderr)
            join_threads(job.readers, self.terminate_grace)
        job.finalized = True

    def _snapshot(self, job: _BackgroundJob) -> ManagedProcessSnapshot:
        returncode = job.handle.process.poll()
        if returncode is None:
            status: ManagedProcessStatus = "running"
        elif job.detached_child:
            status = "failed"
        elif job.stopped:
            status = "stopped"
        elif returncode == 0:
            status = "exited"
        else:
            status = "failed"
        return ManagedProcessSnapshot(
            process_id=job.process_id,
            status=status,
            returncode=returncode,
            stdout=job.stdout.finish(),
            stderr=job.stderr.finish(),
            elapsed_seconds=max(time.monotonic() - job.started_at, 0),
            error_code="managed_process_detached_child" if job.detached_child else None,
        )

    def _get_job(self, process_id: str) -> _BackgroundJob:
        job = self._jobs.get(process_id)
        if job is None:
            raise ManagedProcessError(
                "未知或已失效的受管进程 ID。", code="managed_process_not_found"
            )
        return job

    def _ensure_open(self) -> None:
        if self._closed:
            raise ManagedProcessError("Runtime 已关闭。", code="managed_process_closed")


__all__ = [
    "ManagedProcessError",
    "ManagedProcessRegistry",
    "ManagedProcessSnapshot",
    "ManagedProcessStatus",
]
