"""内置工具的统一工作区与执行环境边界。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from assistant_agent.execution.control import RunControl
from assistant_agent.execution.process import BoundedProcessResult, ProcessSupervisor


class WorkspaceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "workspace_error") -> None:
        super().__init__(message)
        self.code = code


class BaseWorkspace(ABC):
    """文件路径解析和命令执行的统一边界。"""

    backend = "base"
    os_sandboxed = False
    writable = True

    def __init__(
        self,
        root: str | Path,
        *,
        supervisor: ProcessSupervisor,
        control: RunControl,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.supervisor = supervisor
        self.control = control

    @abstractmethod
    def resolve_path(self, value: str | Path) -> Path:
        """把工具参数解析成宿主可访问路径，并执行边界检查。"""

    def execute(
        self,
        command: str | list[str],
        *,
        shell: bool,
        timeout: float,
        max_stream_chars: int,
        cwd: str | Path | None = None,
    ) -> BoundedProcessResult:
        resolved_cwd = self.resolve_path(cwd or self.root)
        return self.supervisor.run(
            command,
            shell=shell,
            timeout=timeout,
            max_stream_chars=max_stream_chars,
            cwd=str(resolved_cwd),
            control=self.control,
        )

    def close(self) -> None:
        self.supervisor.close()


class HostWorkspace(BaseWorkspace):
    """兼容模式：相对路径基于 root，允许访问宿主其他路径。"""

    backend = "host"

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()


class ConfinedWorkspace(HostWorkspace):
    """应用层工作区约束；不等同于 OS 沙盒。"""

    backend = "confined"

    def resolve_path(self, value: str | Path) -> Path:
        path = super().resolve_path(value)
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"路径超出受限工作区：{path}", code="workspace_escape") from exc
        return path


class ReadOnlyWorkspace(ConfinedWorkspace):
    """只允许读取工作区；进程执行也关闭，避免 Shell 绕过文件写入边界。"""

    backend = "read_only"
    writable = False

    def execute(
        self,
        command: str | list[str],
        *,
        shell: bool,
        timeout: float,
        max_stream_chars: int,
        cwd: str | Path | None = None,
    ) -> BoundedProcessResult:
        del command, shell, timeout, max_stream_chars, cwd
        raise WorkspaceError("只读工作区不允许执行进程", code="filesystem_read_only")
