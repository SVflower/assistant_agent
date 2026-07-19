"""Application 用例消费的 Runtime 与 repository 端口。"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from assistant_agent.agent.run.ports import RunCheckpointRepository, RunTelemetry
from assistant_agent.application.models import RunMeta, Session, SessionMeta
from assistant_agent.contracts.capabilities import MCPServerCapability
from assistant_agent.contracts.interactions import InteractionPort
from assistant_agent.tools.ports import ToolTelemetry

if TYPE_CHECKING:
    from assistant_agent.application.runtime import AgentRuntime


class SessionRepository(Protocol):
    def new_session(self, provider: str = "", model: str = "") -> Session: ...

    def save(self, session: Session, messages: list[dict] | None = None) -> None: ...

    def load(self, session_id: str) -> Session: ...

    def list(self) -> list[SessionMeta]: ...

    def delete(self, session_id: str) -> bool: ...


class RunCatalogRepository(RunCheckpointRepository, Protocol):
    def list(self) -> builtins.list[RunMeta]: ...

    def delete(self, run_id: str) -> bool: ...

    def prune(self, max_terminal_runs: int) -> builtins.list[str]: ...


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
