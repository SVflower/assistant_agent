"""AgentLoop 对外事件契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.agent.failures import ActivityPhase, BudgetSnapshot, RunFailure
from assistant_agent.tools.display import ToolDisplay

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

    def __post_init__(self) -> None:
        if self.kind == "reasoning":
            self.sensitive = True
