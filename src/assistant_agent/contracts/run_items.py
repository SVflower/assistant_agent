"""RunItem 运行事实模型。

RunItem 是一次 Run 的可重放时间轴事实。展示层可以把它投影成正文、思考、计划、
轨迹或检查器，但不得把这些投影再次当作独立事实保存。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunItemKind = Literal[
    "user", "plan", "reasoning", "tool", "chart", "output", "assistant", "compaction", "terminal"
]
RunItemStatus = Literal[
    "planned", "started", "streaming", "waiting", "completed", "failed", "cancelled"
]


class RunItem(BaseModel):
    """持久化的单个运行项；内容只保存安全摘要，具体资源通过引用访问。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    item_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    kind: RunItemKind
    status: RunItemStatus
    sequence: int = Field(ge=0)
    parent_item_id: str | None = None
    created_at: str = Field(min_length=1)
    started_at: str | None = None
    completed_at: str | None = None
    summary: str = Field(default="", max_length=16_000)
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class RunItemLifecycleEvent:
    """RunItem 生命周期事件；同一 item_id 的事件由消费者 upsert。"""

    event: Literal["item_started", "item_delta", "item_completed", "item_failed", "item_cancelled"]
    item: RunItem
    delta: str = ""
    contract_version: int = 2


__all__ = ["RunItem", "RunItemKind", "RunItemLifecycleEvent", "RunItemStatus"]
