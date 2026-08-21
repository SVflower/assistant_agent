"""用户已见 reasoning 的受限 Run 展示契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REASONING_PRESENTATION_VERSION: Literal[1] = 1
MAX_REASONING_PRESENTATION_CHARS = 100_000


class ReasoningPresentationV1(BaseModel):
    """只用于 Run 历史展示，不属于 Session 消息或模型上下文。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = REASONING_PRESENTATION_VERSION
    text: str = Field(max_length=MAX_REASONING_PRESENTATION_CHARS)
    started_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    duration_ms: int = Field(ge=0)
    truncated: bool = False


__all__ = [
    "MAX_REASONING_PRESENTATION_CHARS",
    "REASONING_PRESENTATION_VERSION",
    "ReasoningPresentationV1",
]
