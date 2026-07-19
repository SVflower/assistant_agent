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


def test_execution_lease_is_released_after_holder_is_force_killed(tmp_path):
    ready = tmp_path / "ready"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
lease = FileSessionExecutionLeaseManager(Path(sys.argv[1])).acquire('session-1')
Path(sys.argv[2]).write_text('ready', encoding='ascii')
time.sleep(30)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "leases"), str(ready)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(RunStillActiveError):
            FileSessionExecutionLeaseManager(tmp_path / "leases").acquire("session-1")
        process.kill()
        process.wait(timeout=5)
        lease = FileSessionExecutionLeaseManager(tmp_path / "leases").acquire("session-1")
        lease.release()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_concurrent_processes_have_exactly_one_lease_winner(tmp_path):
    start = tmp_path / "start"
    release = tmp_path / "release"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.contracts.errors import RunStillActiveError
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
base, start, release, result = map(Path, sys.argv[1:])
deadline = time.monotonic() + 10
while not start.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
try:
    lease = FileSessionExecutionLeaseManager(base).acquire('session-1')
except RunStillActiveError:
    result.write_text('blocked', encoding='ascii')
else:
    result.write_text('acquired', encoding='ascii')
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    lease.release()
"""
    results = [tmp_path / f"result-{index}" for index in range(6)]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path / "leases"),
                str(start),
                str(release),
                str(result),
            ]
        )
        for result in results
    ]
    try:
        start.touch()
        deadline = time.monotonic() + 7
        while (
            sum(result.exists() for result in results) < len(results)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        outcomes = [result.read_text(encoding="ascii") for result in results if result.exists()]
        assert outcomes.count("acquired") == 1
        assert outcomes.count("blocked") == len(results) - 1
    finally:
        release.touch()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    assert all(process.returncode == 0 for process in processes)
