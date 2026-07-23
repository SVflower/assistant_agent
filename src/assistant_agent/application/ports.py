"""Application 用例消费的 Runtime 与 repository 端口。"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from assistant_agent.agent.run.ports import RunCheckpointRepository, RunTelemetry
from assistant_agent.application.models import RunMeta, Session, SessionMeta
from assistant_agent.contracts.attachments import (
    AttachmentPayloadV1,
    AttachmentRefV1,
    AttachmentSummaryV1,
    AttachmentUploadV1,
)
from assistant_agent.contracts.capabilities import MCPServerCapability
from assistant_agent.contracts.interactions import InteractionPort
from assistant_agent.tools.ports import ToolTelemetry

if TYPE_CHECKING:
    from assistant_agent.application.runtime import AgentRuntime

_T = TypeVar("_T")


class SessionRepository(Protocol):
    def new_session(self, provider: str = "", model: str = "") -> Session: ...

    def save(
        self,
        session: Session,
        messages: list[dict] | None = None,
        *,
        must_exist: bool = True,
    ) -> None: ...

    def load(self, session_id: str) -> Session: ...

    def read_locked(self, session_id: str, reader: Callable[[Session], _T]) -> _T: ...

    def list(self) -> list[SessionMeta]: ...

    def update_metadata(self, session_id: str, title: str, expected_version: int) -> Session: ...

    def fork_session(
        self,
        source_session_id: str,
        before_user_message_id: str,
        key_hash: str,
        request_hash: str,
    ) -> tuple[Session, bool]: ...

    def delete(self, session_id: str) -> bool: ...


class AttachmentRepository(Protocol):
    """Session-scoped immutable attachment storage."""

    def ingest(
        self, session_id: str, uploads: Sequence[AttachmentUploadV1]
    ) -> tuple[AttachmentSummaryV1, ...]: ...

    def get(self, ref: AttachmentRefV1) -> AttachmentPayloadV1: ...

    def get_by_id(self, session_id: str, attachment_id: str) -> AttachmentPayloadV1: ...

    def bind(self, session_id: str, attachment_ids: Sequence[str]) -> None: ...

    def delete_unbound(self, session_id: str, attachment_ids: Sequence[str]) -> int: ...

    def collect_expired(self) -> int: ...

    def delete_session(self, session_id: str) -> None: ...

    def fork(
        self,
        source_session_id: str,
        target_session_id: str,
        refs: Sequence[AttachmentRefV1],
    ) -> dict[str, AttachmentRefV1]: ...


class RunCatalogRepository(RunCheckpointRepository, Protocol):
    def list(self) -> builtins.list[RunMeta]: ...

    def delete(self, run_id: str) -> bool: ...

    def prune(self, max_terminal_runs: int) -> builtins.list[str]: ...

    def delete_session_runs(self, session_id: str) -> builtins.list[str]: ...

    def last_for_session_locked(self, session_id: str) -> RunMeta | None: ...


class SessionExecutionLease(Protocol):
    def release(self) -> None: ...


class SessionExecutionLeaseManager(Protocol):
    def acquire(self, session_id: str) -> SessionExecutionLease: ...


class RuntimeFactoryPort(Protocol):
    def __call__(
        self,
        interaction: InteractionPort | None,
        interactive: bool,
        session_id: str | None,
    ) -> AgentRuntime: ...


class RuntimeTelemetry(RunTelemetry, ToolTelemetry, Protocol):
    def bind_session(self, session_id: str | None) -> None: ...

    def task(self, text: str) -> None: ...

    def session_end(self, *, reason: str = "") -> None: ...


class SkillMetaPort(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def path(self) -> Path: ...


class Closable(Protocol):
    def close(self) -> None: ...


class MCPRuntimePort(Closable, Protocol):
    def server_summary(self) -> list[tuple[str, list[str]]]: ...

    def server_capabilities(self) -> Sequence[MCPServerCapability]: ...


class ExtensionServicePort(Protocol):
    """Runtime 暂存给 CLI 扩展命令的透明 adapter 标记。"""

    def __getattr__(self, name: str) -> Any: ...
