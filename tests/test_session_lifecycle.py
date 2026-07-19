"""Session/Run 共享 lifecycle 锁与 tombstone 回归。"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.session_lifecycle import SessionLifecycle
from assistant_agent.persistence.store import SessionStore


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
