"""M22 单机跨进程 Session execution lease。"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from assistant_agent.contracts.errors import RunStillActiveError
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager


def test_execution_lease_is_exclusive_and_reusable(tmp_path):
    first = FileSessionExecutionLeaseManager(tmp_path).acquire("session-1")
    try:
        with pytest.raises(RunStillActiveError):
            FileSessionExecutionLeaseManager(tmp_path).acquire("session-1")
    finally:
        first.release()
    second = FileSessionExecutionLeaseManager(tmp_path).acquire("session-1")
    second.release()


def test_execution_lease_blocks_another_process_and_child_is_closed(tmp_path):
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
base, ready, release = map(Path, sys.argv[1:])
lease = FileSessionExecutionLeaseManager(base).acquire('session-1')
try:
    ready.write_text('ready', encoding='ascii')
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
finally:
    lease.release()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / "leases"), str(ready), str(release)]
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(RunStillActiveError):
            FileSessionExecutionLeaseManager(tmp_path / "leases").acquire("session-1")
    finally:
        release.touch()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
    assert process.returncode == 0
