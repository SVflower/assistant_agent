"""ReAct 主循环：观察 → 推理 → 调工具 → 再观察，直到任务完成。

这是稳定内核。加能力 = 往 ToolRegistry 注册工具，本文件不应改动。
循环通过 yield StepEvent 把过程暴露给 UI 层，自身不做任何打印。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.agent.context import Conversation
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMClient, LLMError
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry

EventKind = Literal["assistant", "tool_call", "tool_result", "final", "error"]


@dataclass
class StepEvent:
    """循环每一步对外暴露的事件，供 UI 渲染。"""

    kind: EventKind
    text: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] | None = None
    is_error: bool = False


class AgentLoop:
    """驱动一次任务从开始到完成的 ReAct 循环。"""

    def __init__(
        self,
        config: AppConfig,
        client: LLMClient,
        registry: ToolRegistry,
        tool_context: ToolContext,
    ) -> None:
        self._config = config
        self._client = client
        self._registry = registry
        self._tool_ctx = tool_context
        self._conversation = Conversation(max_history_messages=config.agent.max_history_messages)

    def run(self, task: str) -> Iterator[StepEvent]:
        """执行一个任务，逐步 yield 事件。

        循环终止条件：模型不再请求工具（任务完成）、达到最大轮数、或发生错误。
        """
        self._conversation.add_user(task)
        tool_schemas = self._registry.schemas()

        for _ in range(self._config.agent.max_iterations):
            try:
                response = self._client.complete(
                    messages=self._conversation.messages(),
                    tools=tool_schemas,
                )
            except LLMError as exc:
                yield StepEvent(kind="error", text=str(exc), is_error=True)
                return

            # 没有工具调用 → 任务完成，输出最终回复。
            if not response.wants_tools:
                final = response.content or "（模型未返回内容）"
                self._conversation.add_assistant(final)
                yield StepEvent(kind="final", text=final)
                return

            # 有工具调用：先把 assistant 的工具调用消息记入历史。
            raw_tool_calls = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in response.tool_calls
            ]
            self._conversation.add_assistant(response.content, tool_calls=raw_tool_calls)

            if response.content:
                yield StepEvent(kind="assistant", text=response.content)

            # 依次执行每个工具调用，结果写回历史。
            for call in response.tool_calls:
                yield StepEvent(
                    kind="tool_call",
                    tool_name=call.name,
                    tool_args=call.arguments,
                )
                result = self._registry.execute(call.name, call.arguments, self._tool_ctx)
                self._conversation.add_tool_result(call.id, call.name, result.output)
                yield StepEvent(
                    kind="tool_result",
                    tool_name=call.name,
                    text=result.output,
                    is_error=result.is_error,
                )

        # 用尽最大轮数仍未完成。
        yield StepEvent(
            kind="error",
            text=f"已达最大轮数（{self._config.agent.max_iterations}），任务未完成。",
            is_error=True,
        )
