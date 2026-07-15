"""AgentLoop 对外事件契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

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
]


@dataclass
class StepEvent:
    """循环每一步对外暴露的事件，供 UI 渲染。"""

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
