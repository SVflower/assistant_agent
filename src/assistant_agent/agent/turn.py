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

_EMPTY_RESPONSE_RETRY_PROMPT = (
    "上一响应为空。请继续当前任务：返回有效文本，或调用一个可用工具。不要重复已经完成的工具调用。"
)


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
    """保持 provider 流事件顺序，并对完全空响应做一次有界修正。"""
    request_messages = messages
    for attempt in range(2):
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        failure: RunFailure | None = None
        interrupted = ControlState.RUNNING
        saw_content = False

        for event in client.complete_stream(messages=request_messages, tools=tools):
            if event.kind == "reasoning":
                yield StepEvent(kind="reasoning", text=event.text)
            elif event.kind == "content":
                saw_content = saw_content or bool(event.text)
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

        # 空 stream 没有事件可触发上面的检查；重试前必须重新观察 pause/cancel。
        interrupted = control_state()
        if interrupted is not ControlState.RUNNING or failure is not None:
            return ModelTurnResult(
                content="".join(content_parts),
                tool_calls=tool_calls,
                failure=failure,
                control_state=interrupted,
            )
        if saw_content or tool_calls:
            return ModelTurnResult(content="".join(content_parts), tool_calls=tool_calls)
        if attempt == 0:
            request_messages = [
                *messages,
                {"role": "user", "content": _EMPTY_RESPONSE_RETRY_PROMPT},
            ]

    return ModelTurnResult(
        failure=provider_failure(
            "provider_empty_response",
            "模型连续两次未返回可用内容。",
            True,
        )
    )
