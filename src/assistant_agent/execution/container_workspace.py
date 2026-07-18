"""Docker/Podman-backed command isolation for a confined workspace."""

from __future__ import annotations

import os
import shlex
import shutil
import uuid
from pathlib import Path

from assistant_agent.execution.control import RunControl
from assistant_agent.execution.process import BoundedProcessResult, ProcessSupervisor
from assistant_agent.execution.workspace import ConfinedWorkspace, WorkspaceError

_CONTAINER_ROOT = "/workspace"


class ContainerWorkspace(ConfinedWorkspace):
    """Keep file access confined and execute commands in an ephemeral container."""

    backend = "container"
    os_sandboxed = True

    def __init__(
        self,
        root: str | Path,
        *,
        supervisor: ProcessSupervisor,
        control: RunControl,
        engine: str,
        image: str,
        network: str,
        memory: str,
        cpus: float,
        pids_limit: int,
        user: str = "auto",
    ) -> None:
        super().__init__(root, supervisor=supervisor, control=control)
        executable = shutil.which(engine)
        if executable is None:
            raise WorkspaceError(
                f"未找到容器引擎 {engine}，请安装并确保其在 PATH 中",
                code="container_engine_missing",
            )
        self.engine = executable
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.user = _resolve_user(user)
        self.container_name = f"assistant-agent-{uuid.uuid4().hex[:12]}"
        self._started = False
        self._start()

    def execute(
        self,
        command: str | list[str],
        *,
        shell: bool,
        timeout: float,
        max_stream_chars: int,
        cwd: str | Path | None = None,
    ) -> BoundedProcessResult:
        self._ensure_started()
        container_cwd = self._container_path(self.resolve_path(cwd or self.root))
        prefix = [
            self.engine,
            "exec",
            "--workdir",
            container_cwd,
            self.container_name,
        ]
        if shell:
            shell_command = command if isinstance(command, str) else shlex.join(command)
            container_command = ["sh", "-lc", shell_command]
        else:
            container_command = [command] if isinstance(command, str) else command
        result = self.supervisor.run(
            [*prefix, *container_command],
            shell=False,
            timeout=timeout,
            max_stream_chars=max_stream_chars,
            cwd=str(self.root),
            control=self.control,
        )
        if result.timed_out or result.interrupted:
            # Killing docker exec does not guarantee that its container-side process stopped.
            self._destroy()
        return result

    def close(self) -> None:
        try:
            self._destroy()
        finally:
            super().close()

    def _container_path(self, host_path: Path) -> str:
        relative = host_path.relative_to(self.root)
        if not relative.parts:
            return _CONTAINER_ROOT
        return f"{_CONTAINER_ROOT}/{relative.as_posix()}"

    def _ensure_started(self) -> None:
        if not self._started:
            self._start()

    def _start(self) -> None:
        mount = f"type=bind,source={self.root},target={_CONTAINER_ROOT}"
        command = [
            self.engine,
            "run",
            "--detach",
            "--name",
            self.container_name,
            "--mount",
            mount,
            "--workdir",
            _CONTAINER_ROOT,
            "--network",
            self.network,
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            self.user,
            "--entrypoint",
            "sh",
            self.image,
            "-c",
            "while :; do sleep 3600; done",
        ]
        result = self.supervisor.run(
            command,
            shell=False,
            timeout=120,
            max_stream_chars=16_000,
            cwd=str(self.root),
            control=None,
        )
        if result.returncode != 0:
            detail = result.stderr.text.strip() or result.stdout.text.strip() or "未知错误"
            self._remove_container(ignore_failure=True)
            raise WorkspaceError(f"启动隔离容器失败：{detail}", code="container_start_failed")
        health = self.supervisor.run(
            [
                self.engine,
                "inspect",
                "--format",
                "{{.State.Running}}",
                self.container_name,
            ],
            shell=False,
            timeout=30,
            max_stream_chars=4_000,
            cwd=str(self.root),
            control=None,
        )
        if health.returncode != 0 or health.stdout.text.strip().lower() != "true":
            detail = health.stderr.text.strip() or "容器入口进程未保持运行"
            self._remove_container(ignore_failure=True)
            raise WorkspaceError(f"隔离容器健康检查失败：{detail}", code="container_start_failed")
        self._started = True

    def _destroy(self) -> None:
        if self._started:
            self._remove_container()
            self._started = False

    def _remove_container(self, *, ignore_failure: bool = False) -> None:
        result = self.supervisor.run(
            [self.engine, "rm", "--force", self.container_name],
            shell=False,
            timeout=30,
            max_stream_chars=4_000,
            cwd=str(self.root),
            control=None,
        )
        if result.returncode != 0 and not ignore_failure:
            detail = result.stderr.text.strip() or result.stdout.text.strip() or "未知错误"
            raise WorkspaceError(f"销毁隔离容器失败：{detail}", code="container_cleanup_failed")


def _resolve_user(
    configured: str,
    *,
    platform_name: str | None = None,
    getuid=None,
    getgid=None,
) -> str:
    if configured != "auto":
        return configured
    platform_name = platform_name or os.name
    getuid = getuid or getattr(os, "getuid", None)
    getgid = getgid or getattr(os, "getgid", None)
    if platform_name != "nt" and callable(getuid) and callable(getgid):
        uid = getuid()
        if uid != 0:
            return f"{uid}:{getgid()}"
    return "65534:65534"
