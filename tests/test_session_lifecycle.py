"""Session/Run 共享 lifecycle 锁与 tombstone 回归。"""

from __future__ import annotations

import errno
import importlib
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from assistant_agent.persistence import session_lifecycle as lifecycle_module
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
from assistant_agent.persistence.store import SessionStore


def _spawn_lifecycle_worker(lifecycle_dir: str, result_path: str) -> None:
    with SessionLifecycle(lifecycle_dir).lock("session-1"):
        Path(result_path).write_text("acquired", encoding="ascii")


def _waitpid_with_timeout(pid: int, *, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed, status = os.waitpid(pid, os.WNOHANG)
        if completed == pid:
            return status
        time.sleep(0.02)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    raise AssertionError(f"child process {pid} did not exit within {timeout}s")


def _wait_processes(processes: list[subprocess.Popen[str]], *, timeout: float) -> list[str]:
    deadline = time.monotonic() + timeout
    outputs: list[str] = []
    try:
        for process in processes:
            remaining = deadline - time.monotonic()
            stdout, stderr = process.communicate(timeout=max(remaining, 0.1))
            assert process.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    return outputs


def _run_save_contention(tmp_path, *, workers: int, saves_per_worker: int) -> None:
    lifecycle = tmp_path / "lifecycle"
    session_dir = tmp_path / "sessions"
    store = SessionStore(session_dir, lifecycle_dir=lifecycle)
    session = store.new_session()
    store.save(session, [], must_exist=False)
    start = tmp_path / "start"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.persistence.store import SessionStore
session_dir, lifecycle, start = map(Path, sys.argv[1:4])
session_id, worker, total = sys.argv[4], sys.argv[5], int(sys.argv[6])
deadline = time.monotonic() + 30
while not start.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
store = SessionStore(session_dir, lifecycle_dir=lifecycle)
for index in range(total):
    session = store.load(session_id)
    store.save(
        session,
        [{'role': 'user', 'content': f'{worker}-{index}'}],
        must_exist=True,
    )
print(total)
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(session_dir),
                str(lifecycle),
                str(start),
                session.id,
                str(worker),
                str(saves_per_worker),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for worker in range(workers)
    ]
    start.touch()
    assert _wait_processes(processes, timeout=90) == [str(saves_per_worker)] * workers


def _run_document(run_id: str, session_id: str) -> dict:
    return {
        "schema_version": 12,
        "run_id": run_id,
        "session_id": session_id,
        "task": "race",
        "status": "running",
        "phase": "model_pending",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_tombstone_blocks_session_and_run_recreation(tmp_path):
    lifecycle = tmp_path / "lifecycle"
    sessions = SessionStore(tmp_path / "sessions", lifecycle_dir=lifecycle)
    runs = RunStore(tmp_path / "runs", lifecycle_dir=lifecycle)
    session = sessions.new_session()
    sessions.save(session, [], must_exist=False)
    runs.save("run-1", _run_document("run-1", session.id))

    assert sessions.delete(session.id)
    assert runs.delete_session_runs(session.id) == ["run-1"]
    with pytest.raises(FileNotFoundError):
        sessions.save(session, [])
    with pytest.raises(FileNotFoundError):
        runs.save("run-1", _run_document("run-1", session.id))
    assert not sessions._path(session.id).exists()
    assert not list((tmp_path / "runs").glob("run-1*.json"))


def test_run_tombstone_composes_with_later_session_delete(tmp_path):
    lifecycle = tmp_path / "lifecycle"
    sessions = SessionStore(tmp_path / "sessions", lifecycle_dir=lifecycle)
    runs = RunStore(tmp_path / "runs", lifecycle_dir=lifecycle)
    session = sessions.new_session()
    document = _run_document("run-1", session.id)
    sessions.save(session, [], must_exist=False)
    runs.save("run-1", document)

    assert runs.delete("run-1") is True
    assert sessions.delete(session.id) is True
    assert runs.delete_session_runs(session.id) == []
    with pytest.raises(FileNotFoundError):
        runs.save("run-1", document)
    assert not list((tmp_path / "runs").glob("run-1*.json"))


def test_cross_process_run_save_cannot_race_past_delete_tombstone(tmp_path):
    lifecycle = tmp_path / "lifecycle"
    sessions = SessionStore(tmp_path / "sessions", lifecycle_dir=lifecycle)
    runs = RunStore(tmp_path / "runs", lifecycle_dir=lifecycle)
    session = sessions.new_session()
    sessions.save(session, [], must_exist=False)
    start = tmp_path / "start"
    result = tmp_path / "result"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.persistence.run_store import RunStore
lifecycle, run_dir, start, result = map(Path, sys.argv[1:5])
session_id = sys.argv[5]
deadline = time.monotonic() + 10
while not start.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
document = {
        'schema_version': 12, 'run_id': 'run-race', 'session_id': session_id, 'task': 'race',
    'status': 'running', 'phase': 'model_pending',
    'updated_at': '2026-01-01T00:00:00Z',
}
try:
    RunStore(run_dir, lifecycle_dir=lifecycle).save('run-race', document)
except FileNotFoundError:
    result.write_text('blocked', encoding='ascii')
else:
    result.write_text('saved-before-delete', encoding='ascii')
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(lifecycle),
            str(tmp_path / "runs"),
            str(start),
            str(result),
            session.id,
        ]
    )
    try:
        start.touch()
        assert sessions.delete(session.id)
        runs.delete_session_runs(session.id)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0
    assert result.read_text(encoding="ascii") in {"blocked", "saved-before-delete"}
    assert not list((tmp_path / "runs").glob("run-race*.json"))
    with pytest.raises(FileNotFoundError):
        runs.save("run-race", _run_document("run-race", session.id))


def test_lifecycle_lock_holder_exit_releases_cross_process_lock(tmp_path):
    ready = tmp_path / "ready"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
with SessionLifecycle(Path(sys.argv[1])).lock('session-1'):
    Path(sys.argv[2]).write_text('ready', encoding='ascii')
    time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / "lifecycle"), str(ready)]
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        process.kill()
        process.wait(timeout=5)
        lifecycle = SessionLifecycle(tmp_path / "lifecycle")
        with lifecycle.lock("session-1"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_lifecycle_lock_is_reentrant_in_the_same_thread(tmp_path):
    lifecycle = SessionLifecycle(tmp_path / "lifecycle")
    with lifecycle.lock("session-1"):
        with lifecycle.lock("session-1"):
            assert not lifecycle.is_deleted_locked("session-1")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt error contract")
def test_windows_lock_retries_only_bare_eacces_contention(tmp_path, monkeypatch):
    import msvcrt

    calls: list[int] = []
    sleeps: list[float] = []

    def locking(_fd: int, mode: int, _length: int) -> None:
        calls.append(mode)
        if len(calls) == 1:
            raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(msvcrt, "locking", locking)
    monkeypatch.setattr(lifecycle_module.time, "sleep", sleeps.append)
    path = tmp_path / "lock"
    path.write_bytes(b"0")

    with path.open("r+b") as handle:
        lifecycle_module._lock_file(handle)

    assert calls == [msvcrt.LK_NBLCK, msvcrt.LK_NBLCK]
    assert sleeps == [0.05]


def _os_error(err: int | None, *, winerror: int | None = None) -> OSError:
    exc = OSError(err, "injected")
    if winerror is not None:
        exc.winerror = winerror
    return exc


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_os_error(errno.EACCES), True),
        (_os_error(errno.EACCES, winerror=5), False),
        (_os_error(errno.EACCES, winerror=36), False),
        (_os_error(errno.EAGAIN), False),
        (_os_error(errno.EDEADLK), False),
        (_os_error(None, winerror=36), False),
        (_os_error(errno.EBADF), False),
        (_os_error(errno.ENOSPC), False),
    ],
    ids=[
        "contention",
        "access-denied",
        "eacces-winerror-36",
        "eagain",
        "edeadlk",
        "winerror-36",
        "ebadf",
        "enospc",
    ],
)
def test_windows_lock_contention_predicate_fails_closed(error, expected):
    assert lifecycle_module._is_windows_lock_contention(error) is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt error contract")
@pytest.mark.parametrize(
    "error",
    [
        _os_error(errno.EACCES, winerror=5),
        _os_error(errno.EACCES, winerror=36),
        _os_error(errno.EAGAIN),
        _os_error(errno.EDEADLK),
        _os_error(None, winerror=36),
        _os_error(errno.EBADF),
        _os_error(errno.ENOSPC),
    ],
    ids=[
        "access-denied",
        "eacces-winerror-36",
        "eagain",
        "edeadlk",
        "winerror-36",
        "ebadf",
        "enospc",
    ],
)
def test_windows_lock_fails_closed_for_non_contention_errors(tmp_path, monkeypatch, error):
    import msvcrt

    calls = 0
    sleeps: list[float] = []

    def locking(_fd: int, _mode: int, _length: int) -> None:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(msvcrt, "locking", locking)
    monkeypatch.setattr(lifecycle_module.time, "sleep", sleeps.append)
    path = tmp_path / "lock"
    path.write_bytes(b"0")

    with path.open("r+b") as handle, pytest.raises(OSError) as raised:
        lifecycle_module._lock_file(handle)

    assert raised.value is error
    assert calls == 1
    assert sleeps == []


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="POSIX fork contract")
def test_current_thread_cannot_fork_while_holding_lifecycle_lock(tmp_path):
    lifecycle = SessionLifecycle(tmp_path / "lifecycle")

    with lifecycle.lock("session-1"):
        with pytest.raises(RuntimeError, match="持有 lifecycle 锁，禁止 fork"):
            pid = os.fork()
            if pid == 0:
                os._exit(91)
            os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="POSIX fork contract")
def test_fork_waits_for_lifecycle_lock_held_by_another_thread(tmp_path):
    lifecycle = SessionLifecycle(tmp_path / "lifecycle")
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with lifecycle.lock("session-1"):
            entered.set()
            assert release.wait(timeout=10)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert entered.wait(timeout=5)
    releaser = threading.Timer(0.4, release.set)
    releaser.start()
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            with lifecycle.lock("session-1"):
                pass
        except BaseException:
            os._exit(92)
        os._exit(0)
    status = _waitpid_with_timeout(pid, timeout=10)
    elapsed = time.monotonic() - started
    holder.join(timeout=5)
    releaser.join(timeout=5)

    assert not holder.is_alive()
    assert os.waitstatus_to_exitcode(status) == 0
    assert elapsed >= 0.3


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="POSIX fork contract")
def test_parent_and_child_serialize_on_file_lock_after_fork(tmp_path):
    lifecycle_dir = tmp_path / "lifecycle"
    result = tmp_path / "child-elapsed"
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)
        try:
            os.read(read_fd, 1)
            started = time.monotonic()
            with SessionLifecycle(lifecycle_dir).lock("session-1"):
                elapsed = time.monotonic() - started
            result.write_text(str(elapsed), encoding="ascii")
        except BaseException:
            os._exit(93)
        finally:
            os.close(read_fd)
        os._exit(0)

    os.close(read_fd)
    try:
        with SessionLifecycle(lifecycle_dir).lock("session-1"):
            os.write(write_fd, b"1")
            time.sleep(0.5)
    finally:
        os.close(write_fd)
    status = _waitpid_with_timeout(pid, timeout=10)

    assert os.waitstatus_to_exitcode(status) == 0
    assert float(result.read_text(encoding="ascii")) >= 0.3


def test_spawn_process_uses_cross_process_lifecycle_lock(tmp_path):
    lifecycle_dir = tmp_path / "lifecycle"
    result = tmp_path / "spawn-result"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_spawn_lifecycle_worker,
        args=(str(lifecycle_dir), str(result)),
    )

    with SessionLifecycle(lifecycle_dir).lock("session-1"):
        process.start()
        time.sleep(0.2)
        assert process.is_alive()
        assert not result.exists()
    try:
        process.join(timeout=15)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    assert process.exitcode == 0
    assert result.read_text(encoding="ascii") == "acquired"


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="POSIX fork contract")
def test_reload_keeps_single_fork_guard_and_rejects_held_lock(tmp_path):
    reloaded = importlib.reload(lifecycle_module)
    lifecycle = reloaded.SessionLifecycle(tmp_path / "lifecycle")

    with lifecycle.lock("session-1"):
        with pytest.raises(RuntimeError, match="持有 lifecycle 锁，禁止 fork"):
            pid = os.fork()
            if pid == 0:
                os._exit(94)
            os.waitpid(pid, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt contention regression")
def test_windows_two_processes_complete_all_240_contended_saves(tmp_path):
    _run_save_contention(tmp_path, workers=2, saves_per_worker=120)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt contention regression")
def test_windows_eight_processes_sustain_lock_contention_without_failure(tmp_path):
    _run_save_contention(tmp_path, workers=8, saves_per_worker=40)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt contention regression")
def test_windows_waiter_acquires_after_holder_os_exit_without_busy_wait(tmp_path):
    ready = tmp_path / "ready"
    waiter_ready = tmp_path / "waiter-ready"
    lifecycle = tmp_path / "lifecycle"
    holder_script = """
import os, sys, time
from pathlib import Path
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
with SessionLifecycle(Path(sys.argv[1])).lock('session-1'):
    Path(sys.argv[2]).write_text('ready', encoding='ascii')
    deadline = time.monotonic() + 10
    while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not Path(sys.argv[3]).exists():
        raise RuntimeError('waiter did not start')
    time.sleep(1.2)
    os._exit(0)
"""
    waiter_script = """
import json, sys, time
from pathlib import Path
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
Path(sys.argv[2]).write_text('ready', encoding='ascii')
started = time.monotonic()
cpu_started = time.process_time()
with SessionLifecycle(Path(sys.argv[1])).lock('session-1'):
    result = {'elapsed': time.monotonic() - started, 'cpu': time.process_time() - cpu_started}
print(json.dumps(result))
"""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(lifecycle),
            str(ready),
            str(waiter_ready),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    waiter: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        waiter = subprocess.Popen(
            [sys.executable, "-c", waiter_script, str(lifecycle), str(waiter_ready)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        output = _wait_processes([holder, waiter], timeout=15)[1]
    finally:
        for process in (holder, waiter):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    result = json.loads(output)
    assert result["elapsed"] >= 1.0
    assert result["cpu"] < 0.25


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt contention regression")
def test_windows_direct_save_delete_mixed_contention_has_only_lifecycle_outcomes(tmp_path):
    lifecycle = tmp_path / "lifecycle"
    session_dir = tmp_path / "sessions"
    run_dir = tmp_path / "runs"
    sessions = SessionStore(session_dir, lifecycle_dir=lifecycle)
    runs = RunStore(run_dir, lifecycle_dir=lifecycle)
    session = sessions.new_session()
    sessions.save(session, [{"role": "user", "content": "mixed"}], must_exist=False)
    runs.save("run-1", _run_document("run-1", session.id))
    start = tmp_path / "start"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.application.sessions import AgentService
from assistant_agent.contracts.errors import SessionNotFoundError
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.persistence.attachments import AttachmentStore
from assistant_agent.persistence.outputs import OutputStore
from assistant_agent.config.schema import AttachmentsConfig, OutputConfig
mode, session_dir, run_dir, lifecycle, start, session_id = sys.argv[1:7]
session_dir, run_dir, lifecycle, start = map(Path, (session_dir, run_dir, lifecycle, start))
deadline = time.monotonic() + 30
while not start.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
sessions = SessionStore(session_dir, lifecycle_dir=lifecycle)
runs = RunStore(run_dir, lifecycle_dir=lifecycle)
if mode == 'save':
    for index in range(60):
        try:
            session = sessions.load(session_id)
            sessions.save(session, [{'role': 'user', 'content': str(index)}])
        except FileNotFoundError:
            break
elif mode == 'direct':
    attachment_store = AttachmentStore(lifecycle.parent / 'attachments', AttachmentsConfig())
    output_store = OutputStore(lifecycle.parent, OutputConfig())
    service = AgentService(
        runtime_factory=lambda *_args: None,
        session_store=sessions,
        run_store=runs,
        session_leases=FileSessionExecutionLeaseManager(lifecycle.parent / 'leases'),
        max_completed_runs=10,
        attachment_store=attachment_store,
        output_store=output_store,
    )
    for _ in range(60):
        try:
            service.get_session_summary(session_id)
        except SessionNotFoundError:
            break
else:
    time.sleep(0.15)
    if sessions.delete(session_id):
        runs.delete_session_runs(session_id)
print('ok')
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                mode,
                str(session_dir),
                str(run_dir),
                str(lifecycle),
                str(start),
                session.id,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for mode in ("save", "direct", "delete")
    ]
    start.touch()
    assert _wait_processes(processes, timeout=90) == ["ok", "ok", "ok"]
    assert not sessions._path(session.id).exists()
    assert not list(run_dir.glob("run-1*.json"))
