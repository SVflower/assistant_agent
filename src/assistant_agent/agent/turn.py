"""单轮模型流归一化；不决定 Run 终态。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any

from assistant_agent.agent.run.failures import provider_failure
from assistant_agent.agent.run.ports import ControlState
from assistant_agent.contracts.events import StepEvent
from assistant_agent.contracts.failures import RunFailure
from assistant_agent.providers.ports import ModelProviderPort, ToolCall


@dataclass
class ModelTurnResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    failure: RunFailure | None = None
    control_state: ControlState = ControlState.RUNNING


def stream_model_turn(
    client: ModelProviderPort,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    control_state: Callable[[], ControlState],
    content_sink: Callable[[str], None] | None = None,
    emit_content: bool = True,
    collect_content: bool = True,
) -> Generator[StepEvent, None, ModelTurnResult]:
    """保持 provider 流事件顺序，并返回完整单轮事实。"""
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    failure: RunFailure | None = None
    interrupted = ControlState.RUNNING

    for event in client.complete_stream(messages=messages, tools=tools):
        if event.kind == "reasoning":
            yield StepEvent(kind="reasoning", text=event.text)
        elif event.kind == "content":
            if content_sink is not None:
                content_sink(event.text)
            if collect_content:
                content_parts.append(event.text)
            if emit_content:
                yield StepEvent(kind="content_delta", text=event.text)
        elif event.kind == "tool_calls":
            tool_calls = event.tool_calls
        elif event.kind == "usage":
            yield StepEvent(kind="usage", usage=event.usage)
        elif event.kind == "error":
            if event.failure is not None:
                failure = provider_failure(
                    event.failure.code,
                    event.failure.safe_message,
                    event.failure.retryable,
                )
            else:
                failure = RunFailure(
                    code="internal_error",
                    safe_message="模型调用失败。",
                    allowed_actions=("retry_run", "stop"),
                    phase="calling_model",
                    terminal_status="failed",
                )
        interrupted = control_state()
        if interrupted is not ControlState.RUNNING:
            break

    return ModelTurnResult(
        content="".join(content_parts),
        tool_calls=tool_calls,
        failure=failure,
        control_state=interrupted,
    )
