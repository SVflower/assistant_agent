"""单轮模型流归一化；不决定 Run 终态。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from threading import Event, Thread
from typing import Any

from assistant_agent.agent.run.failures import provider_failure
from assistant_agent.agent.run.ports import ControlState
from assistant_agent.contracts.events import StepEvent
from assistant_agent.contracts.failures import RunFailure
from assistant_agent.providers.ports import ModelProviderPort, StreamEvent, ToolCall

_EMPTY_RESPONSE_RETRY_PROMPT = (
    "上一响应为空。请继续当前任务：返回有效文本，或调用一个可用工具。不要重复已经完成的工具调用。"
)


@dataclass
class ModelTurnResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    failure: RunFailure | None = None
    control_state: ControlState = ControlState.RUNNING


@dataclass(frozen=True)
class _StreamRaised:
    error: BaseException


_STREAM_DONE = object()


def _interruptible_stream(
    client: ModelProviderPort,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    control_state: Callable[[], ControlState],
) -> Generator[StreamEvent, None, ControlState]:
    """在独立 daemon 线程消费同步 Provider，使无 chunk 阶段仍可响应控制信号。"""
    queue: SimpleQueue[StreamEvent | _StreamRaised | object] = SimpleQueue()
    abandoned = Event()

    def produce() -> None:
        try:
            for event in client.complete_stream(messages=messages, tools=tools):
                if abandoned.is_set():
                    return
                queue.put(event)
        except BaseException as exc:
            if not abandoned.is_set():
                queue.put(_StreamRaised(exc))
        finally:
            if not abandoned.is_set():
                queue.put(_STREAM_DONE)

    Thread(target=produce, name="assistant-agent-model-stream", daemon=True).start()
    first_poll = True
    while True:
        if not first_poll:
            interrupted = control_state()
            if interrupted is not ControlState.RUNNING:
                abandoned.set()
                return interrupted
        first_poll = False
        try:
            item = queue.get(timeout=0.1)
        except Empty:
            interrupted = control_state()
            if interrupted is ControlState.RUNNING:
                continue
            abandoned.set()
            return interrupted
        if item is _STREAM_DONE:
            return ControlState.RUNNING
        if isinstance(item, _StreamRaised):
            raise item.error
        if not isinstance(item, StreamEvent):
            raise RuntimeError("模型流桥接收到未知事件")
        yield item


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

        provider_stream = _interruptible_stream(
            client,
            messages=request_messages,
            tools=tools,
            control_state=control_state,
        )
        while True:
            try:
                event = next(provider_stream)
            except StopIteration as stopped:
                interrupted = stopped.value
                break
            if event.kind == "reasoning":
                yield StepEvent(kind="reasoning", item_id="item_reasoning", text=event.text)
            elif event.kind == "content":
                saw_content = saw_content or bool(event.text)
                if content_sink is not None:
                    content_sink(event.text)
                if collect_content:
                    content_parts.append(event.text)
                if emit_content:
                    yield StepEvent(kind="content_delta", item_id="item_final", text=event.text)
            elif event.kind == "tool_calls":
                tool_calls = event.tool_calls
            elif event.kind == "usage":
                yield StepEvent(kind="usage", usage=event.usage)
            elif event.kind == "finish" and event.finish_reason == "length":
                failure = provider_failure(
                    "provider_output_truncated",
                    "模型输出达到长度上限，未生成完整结果。",
                    True,
                )
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
