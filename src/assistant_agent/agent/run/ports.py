"""Run 状态机消费的 checkpoint、控制和 telemetry 端口。"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal, Protocol


class ControlState(IntEnum):
    RUNNING = 0
    PAUSE_REQUESTED = 1
    CANCEL_REQUESTED = 2


class RunControlPort(Protocol):
    @property
    def state(self) -> ControlState: ...

    def request_pause(self) -> ControlState: ...

    def request_cancel(self) -> ControlState: ...

    def reset(self) -> None: ...


class LoadedRunPort(Protocol):
    @property
    def document(self) -> dict[str, Any]: ...

    @property
    def source(self) -> Literal["current", "previous"]: ...

    @property
    def warning(self) -> str: ...


class RunCheckpointRepository(Protocol):
    def save(self, run_id: str, document: dict[str, Any]) -> int | None: ...

    def load(self, run_id: str) -> LoadedRunPort: ...


class RunTelemetry(Protocol):
    def run_start(self, *, run_id: str, provider: str, model: str, task: str) -> None: ...

    def run_resume(
        self,
        *,
        run_id: str,
        phase: str,
        source: str,
        provider: str,
        model: str,
        warning: str = "",
    ) -> None: ...

    def run_checkpoint(self, *, run_id: str, status: str, phase: str, iteration: int) -> None: ...

    def run_end(self, *, run_id: str, status: str, reason: str = "") -> None: ...


class NullRunTelemetry:
    def run_start(self, *, run_id: str, provider: str, model: str, task: str) -> None:
        return

    def run_resume(
        self,
        *,
        run_id: str,
        phase: str,
        source: str,
        provider: str,
        model: str,
        warning: str = "",
    ) -> None:
        return

    def run_checkpoint(self, *, run_id: str, status: str, phase: str, iteration: int) -> None:
        return

    def run_end(self, *, run_id: str, status: str, reason: str = "") -> None:
        return
