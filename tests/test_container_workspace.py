"""M14c container workspace command and lifecycle tests."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from assistant_agent.config.schema import SandboxConfig
from assistant_agent.execution import (
    BoundedProcessResult,
    CapturedStream,
    ContainerWorkspace,
    RunControl,
    TerminationReason,
    WorkspaceError,
)
from assistant_agent.execution.container_workspace import _resolve_user


def _result(
    returncode: int = 0,
    *,
    stdout: str = "container-id\n",
    stderr: str = "",
    reason: TerminationReason = TerminationReason.COMPLETED,
) -> BoundedProcessResult:
    return BoundedProcessResult(
        returncode,
        CapturedStream(stdout, len(stdout), True),
        CapturedStream(stderr, len(stderr), True),
        reason,
    )


class _Supervisor:
    def __init__(self, results=None) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.results = list(results or [])
        self.next_result = None
        self.closed = False
        self.removed = False

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[1] == "rm":
            result = self.next_result
            self.next_result = None
            if result is not None:
                if result.returncode == 0:
                    self.removed = True
                return result
            self.removed = True
            return _result()
        if self.results:
            return self.results.pop(0)
        if self.next_result is not None:
            result = self.next_result
            self.next_result = None
            return result
        if command[1] == "inspect":
            if self.removed and command[3] == "{{.State.Status}}":
                return _result(1, stderr="No such container")
            return _result(stdout="true\n")
        return _result()

    def close(self) -> None:
        self.closed = True


def _workspace(tmp_path, monkeypatch, supervisor=None, *, patch_engine=True, **overrides):
    if patch_engine:
        monkeypatch.setattr(shutil, "which", lambda _engine: "/usr/bin/docker")
    values = {
        "engine": "docker",
        "image": "python:3.11-slim",
        "network": "none",
        "memory": "1g",
        "cpus": 1.0,
        "pids_limit": 128,
        "user": "65534:65534",
    }
    values.update(overrides)
    return ContainerWorkspace(
        tmp_path,
        supervisor=supervisor or _Supervisor(),
        control=RunControl(),
        **values,
    )


def test_start_command_has_hardening_and_only_workspace_mount(tmp_path, monkeypatch):
    supervisor = _Supervisor()
    workspace = _workspace(tmp_path, monkeypatch, supervisor)
    command, kwargs = supervisor.calls[0]

    assert kwargs["shell"] is False
    assert command[:2] == ["/usr/bin/docker", "run"]
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert command[command.index("--user") + 1] == "65534:65534"
    assert command[command.index("--memory") + 1] == "1g"
    assert command[command.index("--cpus") + 1] == "1.0"
    assert command[command.index("--pids-limit") + 1] == "128"
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert mounts == [f"type=bind,source={tmp_path.resolve()},target=/workspace"]
    assert "--read-only" in command
    assert command[command.index("--tmpfs") + 1] == "/tmp:rw,nosuid,nodev,size=64m"
    assert all("HOME" not in str(item) and ".ssh" not in str(item) for item in command)
    workspace.close()


def test_exec_maps_cwd_and_never_uses_host_shell(tmp_path, monkeypatch):
    (tmp_path / "nested").mkdir()
    supervisor = _Supervisor()
    workspace = _workspace(tmp_path, monkeypatch, supervisor)

    workspace.execute(
        "printf ok", shell=True, timeout=5, max_stream_chars=100, cwd=tmp_path / "nested"
    )
    shell_command, shell_kwargs = supervisor.calls[2]
    assert shell_command[-3:] == ["sh", "-lc", "printf ok"]
    assert shell_command[shell_command.index("--workdir") + 1] == "/workspace/nested"
    assert shell_kwargs["shell"] is False

    workspace.execute(["git", "status"], shell=False, timeout=5, max_stream_chars=100)
    direct_command, _ = supervisor.calls[3]
    assert direct_command[-2:] == ["git", "status"]
    workspace.close()


def test_interruption_destroys_and_next_exec_restarts(tmp_path, monkeypatch):
    supervisor = _Supervisor()
    workspace = _workspace(tmp_path, monkeypatch, supervisor)
    supervisor.next_result = _result(reason=TerminationReason.PAUSED)

    workspace.execute("sleep 10", shell=True, timeout=20, max_stream_chars=100)
    assert supervisor.calls[3][0][1:3] == ["rm", "--force"]

    workspace.execute("echo resumed", shell=True, timeout=5, max_stream_chars=100)
    assert supervisor.calls[5][0][1] == "run"
    assert supervisor.calls[6][0][1] == "inspect"
    assert supervisor.calls[7][0][1] == "exec"
    workspace.close()


def test_close_is_idempotent_and_closes_supervisor(tmp_path, monkeypatch):
    supervisor = _Supervisor()
    workspace = _workspace(tmp_path, monkeypatch, supervisor)
    workspace.close()
    workspace.close()
    remove_calls = [call for call, _ in supervisor.calls if call[1] == "rm"]
    assert len(remove_calls) == 1
    assert supervisor.closed is True


def test_cleanup_failure_is_reported_and_supervisor_still_closes(tmp_path, monkeypatch):
    supervisor = _Supervisor()
    workspace = _workspace(tmp_path, monkeypatch, supervisor)
    supervisor.next_result = _result(1, stderr="remove failed")
    with pytest.raises(WorkspaceError, match="remove failed") as failed:
        workspace.close()
    assert failed.value.code == "container_cleanup_failed"
    assert supervisor.closed is True


def test_missing_engine_and_start_failure_are_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _engine: None)
    with pytest.raises(WorkspaceError) as missing:
        _workspace(tmp_path, monkeypatch, patch_engine=False)
    assert missing.value.code == "container_engine_missing"

    supervisor = _Supervisor()
    supervisor.next_result = _result(125, stderr="daemon unavailable")
    monkeypatch.setattr(shutil, "which", lambda _engine: "/usr/bin/docker")
    with pytest.raises(WorkspaceError, match="daemon unavailable") as failed:
        _workspace(tmp_path, monkeypatch, supervisor)
    assert failed.value.code == "container_start_failed"
    assert supervisor.calls[-2][0][1:3] == ["rm", "--force"]

    unhealthy = _Supervisor([_result(), _result(stdout="false\n")])
    with pytest.raises(WorkspaceError, match="健康检查") as failed_health:
        _workspace(tmp_path, monkeypatch, unhealthy)
    assert failed_health.value.code == "container_start_failed"
    assert unhealthy.calls[-2][0][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("user", ["root", "0", "000:1000", " 0:1 "])
def test_container_config_rejects_root_user(user):
    with pytest.raises(ValueError, match="不能使用 root"):
        SandboxConfig(user=user)


def test_auto_user_never_returns_root():
    assert (
        _resolve_user("auto", platform_name="posix", getuid=lambda: 0, getgid=lambda: 0)
        == "65534:65534"
    )


def test_real_docker_lifecycle_when_engine_and_image_are_available(tmp_path):
    engine = shutil.which("docker")
    if engine is None:
        pytest.skip("Docker 不可用")
    image = "python:3.11-slim"
    inspected = subprocess.run(
        [engine, "image", "inspect", image],
        capture_output=True,
        check=False,
        timeout=20,
    )
    if inspected.returncode != 0:
        pytest.skip(f"本地没有测试镜像 {image}")

    from assistant_agent.execution import ProcessSupervisor

    workspace = ContainerWorkspace(
        tmp_path,
        supervisor=ProcessSupervisor(poll_interval=0.01),
        control=RunControl(),
        engine="docker",
        image=image,
        network="none",
        memory="256m",
        cpus=0.5,
        pids_limit=64,
    )
    name = workspace.container_name
    result = workspace.execute(
        ["python", "-c", "print('isolated')"],
        shell=False,
        timeout=20,
        max_stream_chars=1_000,
    )
    assert result.returncode == 0 and result.stdout.text.strip() == "isolated"
    workspace.close()
    inspected = subprocess.run(
        [engine, "inspect", name], capture_output=True, check=False, timeout=20
    )
    assert inspected.returncode != 0
