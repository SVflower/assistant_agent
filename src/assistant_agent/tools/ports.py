"""工具系统消费的基础设施端口。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from assistant_agent.contracts.events import ToolDisplay
from assistant_agent.tools.models import ArtifactRef, ToolResult

if TYPE_CHECKING:
    from assistant_agent.tools.context import ToolContext
    from assistant_agent.tools.permissions import PermissionRequest


class CapturedStreamPort(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def total_bytes(self) -> int: ...

    @property
    def complete(self) -> bool: ...


class TerminationReasonPort(Protocol):
    @property
    def value(self) -> str: ...


class ProcessResultPort(Protocol):
    @property
    def returncode(self) -> int: ...

    @property
    def stdout(self) -> CapturedStreamPort: ...

    @property
    def stderr(self) -> CapturedStreamPort: ...

    @property
    def termination_reason(self) -> TerminationReasonPort: ...

    @property
    def complete(self) -> bool: ...

    @property
    def timed_out(self) -> bool: ...

    @property
    def interrupted(self) -> bool: ...

    @property
    def background_process(self) -> bool: ...

    @property
    def execution_duration_ms(self) -> int: ...

    @property
    def drain_duration_ms(self) -> int: ...

    @property
    def cleanup_duration_ms(self) -> int: ...


class RunControlPort(Protocol):
    @property
    def state(self) -> Any: ...

    def request_pause(self) -> Any: ...

    def request_cancel(self) -> Any: ...

    def reset(self) -> None: ...


class ProcessSupervisorPort(Protocol):
    def close(self) -> None: ...


class ManagedProcessSnapshotPort(Protocol):
    @property
    def process_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> CapturedStreamPort: ...

    @property
    def stderr(self) -> CapturedStreamPort: ...

    @property
    def elapsed_seconds(self) -> float: ...

    @property
    def error_code(self) -> str | None: ...


class ManagedProcessRegistryPort(Protocol):
    def start(self, command: str, *, cwd: str) -> ManagedProcessSnapshotPort: ...

    def get(self, process_id: str) -> ManagedProcessSnapshotPort: ...

    def list(self) -> Sequence[ManagedProcessSnapshotPort]: ...

    def stop(self, process_id: str) -> ManagedProcessSnapshotPort: ...

    def close(self) -> None: ...


class WorkspacePort(Protocol):
    @property
    def backend(self) -> str: ...

    @property
    def root(self) -> Path: ...

    def resolve_path(self, value: str | Path) -> Path: ...

    def execute(
        self,
        command: str | list[str],
        *,
        shell: bool,
        timeout: float,
        max_stream_chars: int,
        cwd: str | Path | None = None,
    ) -> ProcessResultPort: ...

    def close(self) -> None: ...


class ArtifactStorePort(Protocol):
    def write_text(
        self, content: str, *, prefix: str = "tool-output", complete: bool = True
    ) -> ArtifactRef: ...


class ToolTelemetry(Protocol):
    def correlation_context(self) -> dict[str, str]: ...

    def confirm(self, *, category: str, decision: str, remembered: bool) -> None: ...

    def permission_decision(
        self,
        *,
        mode: str,
        tool: str,
        capabilities: list[str],
        targets: list[str],
        decision: str,
        reason: str,
        remembered: bool,
        matched_rules: list[str],
    ) -> None: ...

    def tool_call(
        self,
        *,
        name: str,
        args: dict[str, Any],
        duration_ms: int,
        status: str,
        output: str,
        approval_wait_ms: int | None = None,
        truncated: bool = False,
        wall_duration_ms: int | None = None,
        execution_duration_ms: int | None = None,
        returned_output_len: int | None = None,
        call_id: str = "",
    ) -> None: ...

    def observer_error(self, *, phase: str, tool: str, error: str) -> None: ...

    def budget_exhausted(
        self, *, reason: str, limit: int, used: int, skipped_calls: int
    ) -> None: ...


class ToolPort(Protocol):
    name: str
    description: str

    @property
    def parameters(self) -> dict[str, Any]: ...

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    def to_schema(self) -> dict[str, Any]: ...

    def display_call(self, args: dict[str, Any]) -> ToolDisplay: ...

    def display_result(self, args: dict[str, Any], result: ToolResult) -> ToolDisplay: ...

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]: ...


class ToolRegistryPort(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...

    def display_call(self, name: str, args: dict[str, Any]) -> ToolDisplay: ...

    def display_result(
        self, name: str, args: dict[str, Any], result: ToolResult
    ) -> ToolDisplay: ...

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        *,
        call_id: str = "",
        lifecycle: Any | None = None,
    ) -> ToolResult: ...
