"""AgentLoop 对外事件契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.contracts.charts import ChartArtifact
from assistant_agent.contracts.failures import ActivityPhase, BudgetSnapshot, RunFailure


@dataclass(frozen=True)
class ToolPreview:
    kind: Literal["code", "diff"]
    content: str
    language: str = "text"
    total_lines: int = 0
    shown_lines: int = 0
    added_lines: int = 0
    removed_lines: int = 0


@dataclass(frozen=True)
class ToolDisplay:
    action: str
    target: str = ""
    summary: str = ""
    detail: str = ""
    preview: ToolPreview | None = None
    importance: Literal["routine", "change", "external"] = "routine"
    timeout_seconds: float | None = None


EventKind = Literal[
    "reasoning",
    "content_delta",
    "tool_call",
    "tool_result",
    "usage",
    "final",
    "error",
    "interrupted",
    "notice",
    "run_terminal",
    "activity",
]
TerminalStatus = Literal["completed", "failed", "paused", "cancelled"]
EVENT_CONTRACT_VERSION = 1


@dataclass
class StepEvent:
    """循环和服务门面对外暴露的向后兼容事件。"""

    kind: EventKind
    text: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] | None = None
    is_error: bool = False
    usage: dict[str, int] | None = None
    call_id: str = ""
    display: ToolDisplay | None = None
    result_code: str = ""
    result_metadata: dict[str, Any] | None = None
    contract_version: int = EVENT_CONTRACT_VERSION
    sensitive: bool = False
    terminal_status: TerminalStatus | None = None
    failure: RunFailure | None = None
    phase: ActivityPhase | None = None
    budget: BudgetSnapshot | None = None
    chart: ChartArtifact | None = None

    def __post_init__(self) -> None:
        if self.kind == "reasoning":
            self.sensitive = True
