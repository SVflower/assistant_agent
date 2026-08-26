"""AgentLoop 对外事件契约。

StepEvent 把运行内核与 CLI/API 解耦：内核只产生结构化事实，调用方自行渲染或映射到网络事件，
不得通过日志或中文错误文本推断状态。新增可选字段保持 v1 向后兼容；破坏性语义变化才提升版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.contracts.charts import ChartArtifactV2
from assistant_agent.contracts.failures import ActivityPhase, BudgetSnapshot, RunFailure
from assistant_agent.contracts.observability import RunObservabilitySnapshot, TrajectoryEntry
from assistant_agent.contracts.outputs import OutputArtifactV1


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
    """循环和服务门面对外暴露的向后兼容事件。

    ``final`` 只承载完整 assistant 正文，Run 是否结束只看唯一 ``run_terminal`` 及其
    ``terminal_status``。``call_id`` 用于稳定配对 tool_call/tool_result，``display`` 是已脱敏的
    展示摘要，服务端不应优先转发原始 ``tool_args``。
    """

    kind: EventKind
    # Additive identity for consumers that reconcile a run as structured items.
    # Existing callers may ignore it and continue using kind/call_id.
    item_id: str | None = None
    item_kind: str | None = None
    item_status: str | None = None
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
    chart: ChartArtifactV2 | None = None
    output: OutputArtifactV1 | None = None
    observability: RunObservabilitySnapshot | None = None
    trajectory_entry: TrajectoryEntry | None = None

    def __post_init__(self) -> None:
        if self.item_id is None:
            if self.kind == "reasoning":
                self.item_id = "item_reasoning"
            elif self.kind == "content_delta" or self.kind == "final":
                self.item_id = "item_final"
            elif self.kind in {"tool_call", "tool_result"} and self.call_id:
                self.item_id = f"item_tool_{self.call_id}"
            elif self.kind == "run_terminal":
                self.item_id = "item_terminal"
        if self.item_kind is None:
            self.item_kind = {
                "reasoning": "reasoning",
                "content_delta": "assistant",
                "final": "assistant",
                "tool_call": "tool",
                "tool_result": "tool",
                "run_terminal": "terminal",
            }.get(self.kind)
        if self.item_status is None:
            self.item_status = {
                "reasoning": "streaming",
                "content_delta": "streaming",
                "final": "completed",
                "tool_call": "started",
                "tool_result": "failed" if self.is_error else "completed",
                "run_terminal": self.terminal_status,
            }.get(self.kind)
        if self.kind == "reasoning":
            self.sensitive = True
