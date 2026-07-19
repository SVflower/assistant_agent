"""Session/Run 共享 lifecycle 锁与 tombstone 回归。"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
from assistant_agent.persistence.store import SessionStore


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
    'run_id': 'run-race', 'session_id': session_id, 'task': 'race',
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt contention regression")
def test_windows_two_processes_complete_all_240_contended_saves(tmp_path):
    _run_save_contention(tmp_path, workers=2, saves_per_worker=120)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt contention regression")
def test_windows_eight_processes_sustain_lock_contention_without_failure(tmp_path):
    _run_save_contention(tmp_path, workers=8, saves_per_worker=40)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows msvcrt contention regression")
def test_windows_waiter_acquires_after_holder_os_exit_without_busy_wait(tmp_path):
    ready = tmp_path / "ready"
    lifecycle = tmp_path / "lifecycle"
    holder_script = """
import os, sys, time
from pathlib import Path
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
with SessionLifecycle(Path(sys.argv[1])).lock('session-1'):
    Path(sys.argv[2]).write_text('ready', encoding='ascii')
    time.sleep(1.5)
    os._exit(0)
"""
    waiter_script = """
import json, sys, time
from pathlib import Path
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
started = time.monotonic()
cpu_started = time.process_time()
with SessionLifecycle(Path(sys.argv[1])).lock('session-1'):
    result = {'elapsed': time.monotonic() - started, 'cpu': time.process_time() - cpu_started}
print(json.dumps(result))
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(lifecycle), str(ready)],
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
            [sys.executable, "-c", waiter_script, str(lifecycle)],
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
    service = AgentService(
        runtime_factory=lambda *_args: None,
        session_store=sessions,
        run_store=runs,
        session_leases=FileSessionExecutionLeaseManager(lifecycle.parent / 'leases'),
        max_completed_runs=10,
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
