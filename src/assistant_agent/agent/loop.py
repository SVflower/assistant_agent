"""ReAct 主循环：观察 → 推理 → 调工具 → 再观察，直到任务完成。

稳定内核。加能力优先往 ToolRegistry 注册工具，尽量不动本文件。
循环消费 LLMClient.complete_stream 的增量，通过 yield StepEvent 把过程
暴露给 UI 层，自身不做任何打印。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.agent.context import Conversation
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMClient, ToolCall
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry

EventKind = Literal[
    "reasoning",
    "content_delta",
    "assistant",
    "tool_call",
    "tool_result",
    "usage",
    "final",
    "error",
    "interrupted",
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


class AgentLoop:
    """驱动一次任务从开始到完成的 ReAct 循环。"""

    def __init__(
        self,
        config: AppConfig,
        client: LLMClient,
        registry: ToolRegistry,
        tool_context: ToolContext,
        interactive: bool = True,
        interrupt_check: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._registry = registry
        self._tool_ctx = tool_context
        # 中断检查：返回 True 表示用户请求中断。默认从不中断。
        self._interrupt_check = interrupt_check
        self._conversation = Conversation(
            max_history_messages=config.agent.max_history_messages,
            max_context_tokens=config.agent.max_context_tokens,
            interactive=interactive,
        )

    def _interrupted(self) -> bool:
        return self._interrupt_check is not None and self._interrupt_check()

    def set_client(self, client: LLMClient) -> None:
        """替换模型客户端（用于对话中切换 provider/模型）。

        仅换客户端，_conversation 不动——切换后对话历史完整保留。
        不改 run() 控制流。
        """
        self._client = client

    def export_history(self) -> list[dict[str, Any]]:
        """导出对话历史（供会话持久化）。"""
        return self._conversation.export_history()

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        """载入对话历史（供恢复会话）。"""
        self._conversation.load_history(messages)

    def run(self, task: str) -> Iterator[StepEvent]:
        """执行一个任务，逐步 yield 事件（流式）。

        循环终止条件：模型不再请求工具（任务完成）、达到最大轮数、或发生错误。
        每一轮通过 complete_stream 消费增量：思考、正文、工具调用、用量。
        """
        self._conversation.add_user(task)
        tool_schemas = self._registry.schemas()

        for _ in range(self._config.agent.max_iterations):
            # 累积本轮的正文与工具调用，供历史写回（中断时用已累积内容，保持"所见即所存"）。
            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            stream_error: str | None = None
            interrupted = False

            for event in self._client.complete_stream(
                messages=self._conversation.messages(),
                tools=tool_schemas,
            ):
                if event.kind == "reasoning":
                    yield StepEvent(kind="reasoning", text=event.text)
                elif event.kind == "content":
                    content_parts.append(event.text)
                    yield StepEvent(kind="content_delta", text=event.text)
                elif event.kind == "tool_calls":
                    tool_calls = event.tool_calls
                elif event.kind == "usage":
                    yield StepEvent(kind="usage", usage=event.usage)
                elif event.kind == "error":
                    stream_error = event.text
                # 流式过程中检查中断：保留已输出、干净停下。
                if self._interrupted():
                    interrupted = True
                    break

            content = "".join(content_parts)

            # 用户中断：把已输出的正文写回历史（不含未完成的工具调用），干净终止。
            if interrupted:
                if content:
                    self._conversation.add_assistant(content)
                yield StepEvent(kind="interrupted", text="已中断（用户请求停止）")
                return

            # 流中途出错：保留已输出内容并写回历史，本轮标记失败终止。
            if stream_error is not None:
                if content:
                    self._conversation.add_assistant(content)
                yield StepEvent(kind="error", text=stream_error, is_error=True)
                return

            # 没有工具调用 → 任务完成，输出最终回复。
            if not tool_calls:
                final = content or "（模型未返回内容）"
                self._conversation.add_assistant(final)
                yield StepEvent(kind="final", text=final)
                return

            # 执行工具批次前检查中断：此时尚未写入 tool_calls 消息，可安全终止。
            # （不在工具批次中途中断——那会留下无结果的 tool_call，破坏下一轮请求。）
            if self._interrupted():
                if content:
                    self._conversation.add_assistant(content)
                yield StepEvent(kind="interrupted", text="已中断（用户请求停止）")
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
                for call in tool_calls
            ]
            self._conversation.add_assistant(content or None, tool_calls=raw_tool_calls)

            # 依次执行每个工具调用，结果写回历史。
            for call in tool_calls:
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
