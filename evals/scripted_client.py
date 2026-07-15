"""把声明式 script 转换为项目原生 StreamEvent 的确定性 client。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from assistant_agent.llm.client import StreamEvent, ToolCall
from evals.schema import ScriptRound


class ScriptedClient:
    def __init__(self, rounds: list[ScriptRound]) -> None:
        self._rounds = rounds
        self.calls = 0

    def complete_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Iterator[StreamEvent]:
        if self.calls >= len(self._rounds):
            yield StreamEvent(kind="error", text="[eval_script_exhausted] scripted 轮次已耗尽")
            return
        round_ = self._rounds[self.calls]
        self.calls += 1
        if round_.reasoning:
            yield StreamEvent(kind="reasoning", text=round_.reasoning)
        if round_.usage:
            yield StreamEvent(kind="usage", usage=round_.usage)
        if round_.error is not None:
            yield StreamEvent(kind="error", text=round_.error)
            return
        if round_.tool_calls:
            calls = [
                ToolCall(
                    id=call.id or f"eval-{self.calls}-{index}",
                    name=call.name,
                    arguments=call.arguments,
                )
                for index, call in enumerate(round_.tool_calls, start=1)
            ]
            yield StreamEvent(kind="tool_calls", tool_calls=calls)
            return
        yield StreamEvent(kind="content", text=round_.final or "")
