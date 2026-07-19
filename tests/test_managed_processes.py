"""M21 受管后台进程生命周期。"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time

import pytest

from assistant_agent.execution.jobs import ManagedProcessError, ManagedProcessRegistry
from assistant_agent.tools.processes import ManageProcessTool
from tests.support import ToolContextFixture


def _command(code: str) -> str:
    parts = [sys.executable, "-u", "-c", code]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def _wait_for_status(manager: ManagedProcessRegistry, process_id: str, status: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = manager.get(process_id)
        if snapshot.status == status:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"进程未进入 {status}")


def test_managed_process_start_logs_and_stop(tmp_path):
    manager = ManagedProcessRegistry(terminate_grace=0.1)
    try:
        started = manager.start(
            _command("import time; print('ready', flush=True); time.sleep(30)"),
            cwd=str(tmp_path),
        )
        assert started.status == "running"
        deadline = time.monotonic() + 2
        while "ready" not in manager.get(started.process_id).stdout.text:
            assert time.monotonic() < deadline
            time.sleep(0.02)

        stopped = manager.stop(started.process_id)
        assert stopped.status == "stopped"
        assert "ready" in stopped.stdout.text
    finally:
        manager.close()


def test_managed_process_natural_exit_and_bounded_output(tmp_path):
    manager = ManagedProcessRegistry(max_stream_chars=100)
    try:
        started = manager.start(_command("print('x' * 10000)"), cwd=str(tmp_path))
        completed = _wait_for_status(manager, started.process_id, "exited")
        assert completed.returncode == 0
        assert completed.stdout.total_bytes >= 10_000
        assert completed.stdout.complete is False
        assert len(completed.stdout.text) < 300
    finally:
        manager.close()


def test_managed_process_limit_and_unknown_id(tmp_path):
    manager = ManagedProcessRegistry(max_processes=1, terminate_grace=0.1)
    try:
        manager.start(_command("import time; time.sleep(30)"), cwd=str(tmp_path))
        with pytest.raises(ManagedProcessError) as limit:
            manager.start(_command("print('no')"), cwd=str(tmp_path))
        assert limit.value.code == "managed_process_limit"
        with pytest.raises(ManagedProcessError) as missing:
            manager.get("proc-000000000000")
        assert missing.value.code == "managed_process_not_found"
    finally:
        manager.close()


def test_runtime_process_registries_are_isolated(tmp_path):
    first = ManagedProcessRegistry(terminate_grace=0.1)
    second = ManagedProcessRegistry(terminate_grace=0.1)
    try:
        started = first.start(_command("import time; time.sleep(30)"), cwd=str(tmp_path))
        with pytest.raises(ManagedProcessError, match="未知"):
            second.get(started.process_id)
        assert second.list() == []
    finally:
        first.close()
        second.close()


def test_managed_process_cleans_detached_child_and_reports_failure(tmp_path):
    marker = tmp_path / "detached-child-survived.txt"
    child = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('alive')"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    manager = ManagedProcessRegistry(terminate_grace=0.1)
    try:
        started = manager.start(_command(parent), cwd=str(tmp_path))
        failed = _wait_for_status(manager, started.process_id, "failed")
        assert failed.error_code == "managed_process_detached_child"
        time.sleep(1.2)
        assert not marker.exists()
    finally:
        manager.close()


def test_close_kills_background_process_tree_and_is_idempotent(tmp_path):
    marker = tmp_path / "survived.txt"
    manager = ManagedProcessRegistry(terminate_grace=0.1)
    manager.start(
        _command(f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('alive')"),
        cwd=str(tmp_path),
    )
    manager.close()
    manager.close()
    time.sleep(1.2)
    assert not marker.exists()
    assert not any(
        thread.name.startswith("assistant-agent-process-") for thread in threading.enumerate()
    )


def test_manage_process_tool_validates_detach_and_returns_opaque_id(tmp_path):
    manager = ManagedProcessRegistry(terminate_grace=0.1)
    ctx = ToolContextFixture(workspace_root=tmp_path, process_manager=manager)
    tool = ManageProcessTool()
    try:
        rejected = tool.run({"action": "start", "command": "start /b python server.py"}, ctx)
        assert rejected.code == "invalid_arguments"

        started = tool.run(
            {"action": "start", "command": _command("import time; time.sleep(30)")}, ctx
        )
        process_id = started.metadata["process_id"]
        assert process_id.startswith("proc-")
        assert "pid" not in started.metadata

        listed = tool.run({"action": "list"}, ctx)
        assert process_id in listed.output
        stopped = tool.run({"action": "stop", "process_id": process_id}, ctx)
        assert stopped.metadata["status"] == "stopped"
    finally:
        manager.close()


def test_manage_process_permission_scope_uses_command_and_opaque_id(tmp_path):
    manager = ManagedProcessRegistry()
    ctx = ToolContextFixture(workspace_root=tmp_path, process_manager=manager)
    tool = ManageProcessTool()
    try:
        start = tool.permission_requests({"action": "start", "command": "python server.py"}, ctx)
        assert {item.capability.value for item in start} == {
            "process.execute",
            "filesystem.write",
            "network.access",
        }
        stop = tool.permission_requests({"action": "stop", "process_id": "proc-000000000000"}, ctx)
        assert len(stop) == 1
        assert stop[0].target == "proc-000000000000"
    finally:
        manager.close()


def test_closed_process_registry_rejects_operations(tmp_path):
    manager = ManagedProcessRegistry()
    manager.close()
    with pytest.raises(ManagedProcessError) as closed:
        manager.list()
    assert closed.value.code == "managed_process_closed"


def test_completed_process_history_is_bounded(tmp_path):
    manager = ManagedProcessRegistry(max_processes=1)
    try:
        for _ in range(7):
            started = manager.start(_command("pass"), cwd=str(tmp_path))
            _wait_for_status(manager, started.process_id, "exited")
        assert len(manager.list()) <= 4
    finally:
        manager.close()
