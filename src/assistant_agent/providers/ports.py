"""模型调用的消费方端口与厂商无关数据。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class LLMError(Exception):
    """模型调用失败。"""


ProviderFailureCode = Literal[
    "provider_rate_limited",
    "provider_unavailable",
    "provider_timeout",
    "provider_empty_response",
    "internal_error",
]


@dataclass(frozen=True)
class ProviderFailure:
    code: ProviderFailureCode
    safe_message: str
    retryable: bool


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


StreamEventKind = Literal["reasoning", "content", "tool_calls", "usage", "error"]


@dataclass
class StreamEvent:
    kind: StreamEventKind
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    failure: ProviderFailure | None = None


class ModelProviderPort(Protocol):
    """Agent 与上下文压缩器依赖的最小同步模型端口。"""

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamEvent]: ...
