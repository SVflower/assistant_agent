"""M14a 运行控制与进程树监管。"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time

from assistant_agent.config.schema import MCPConfig
from assistant_agent.main import _interruptible, _run_control
from assistant_agent.mcp.manager import MCPManager
from assistant_agent.runtime import ControlState, ProcessSupervisor, RunControl, RunInterrupted


def test_run_control_only_upgrades_and_resets():
    control = RunControl()
    assert control.state is ControlState.RUNNING
    assert control.request_interrupt() is ControlState.PAUSE_REQUESTED
    assert control.request_pause() is ControlState.PAUSE_REQUESTED
    assert control.request_interrupt() is ControlState.CANCEL_REQUESTED
    assert control.request_pause() is ControlState.CANCEL_REQUESTED
    control.reset()
    assert control.state is ControlState.RUNNING


def test_process_supervisor_responds_to_pause():
    control = RunControl()
    supervisor = ProcessSupervisor(poll_interval=0.01, terminate_grace=0.1)
    timer = threading.Timer(0.15, control.request_pause)
    timer.start()
    started = time.perf_counter()
    try:
        result = supervisor.run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            shell=False,
            timeout=20,
            max_stream_chars=1000,
            control=control,
        )
    finally:
        timer.cancel()
        supervisor.close()
    assert result.termination_reason.value == "paused"
    assert time.perf_counter() - started < 3


def test_process_supervisor_terminates_descendant_tree(tmp_path):
    marker = tmp_path / "child-survived.txt"
    child_code = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('alive')"
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10)"
    )
    control = RunControl()
    supervisor = ProcessSupervisor(poll_interval=0.01, terminate_grace=0.1)
    timer = threading.Timer(0.2, control.request_cancel)
    timer.start()
    try:
        result = supervisor.run(
            [sys.executable, "-c", parent_code],
            shell=False,
            timeout=20,
            max_stream_chars=1000,
            control=control,
        )
    finally:
        timer.cancel()
        supervisor.close()
    assert result.termination_reason.value == "cancelled"
    time.sleep(1.2)
    assert not marker.exists(), f"{os.name} 子进程未被完整清理"


def test_mcp_future_wait_responds_to_control():
    control = RunControl()
    manager = MCPManager(MCPConfig(), None, run_control=control)
    timer = threading.Timer(0.1, control.request_pause)
    timer.start()
    try:
        try:
            manager._submit(asyncio.sleep(10), timeout=20)
        except RunInterrupted as exc:
            assert exc.cancelled is False
        else:
            raise AssertionError("MCP Future 未响应暂停")
    finally:
        timer.cancel()
        manager.close()


def test_cli_sigint_escalates_from_pause_to_cancel():
    with _interruptible():
        signal.raise_signal(signal.SIGINT)
        assert _run_control.state is ControlState.PAUSE_REQUESTED
        signal.raise_signal(signal.SIGINT)
        assert _run_control.state is ControlState.CANCEL_REQUESTED


def test_timeout_terminates_descendant_tree(tmp_path):
    marker = tmp_path / "timeout-child-survived.txt"
    child_code = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('alive')"
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10)"
    )
    supervisor = ProcessSupervisor(poll_interval=0.01, terminate_grace=0.1)
    try:
        result = supervisor.run(
            [sys.executable, "-c", parent_code],
            shell=False,
            timeout=0.2,
            max_stream_chars=1000,
        )
    finally:
        supervisor.close()
    assert result.termination_reason.value == "timeout"
    time.sleep(1.2)
    assert not marker.exists()
