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

from assistant_agent.agent.context import Conversation, estimate_tools_tokens
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMClient, ToolCall
from assistant_agent.tools.base import ToolBudget, ToolContext
from assistant_agent.tools.registry import ToolRegistry

# 连续多少轮完全相同的工具调用判定为卡死并熔断。
_REPEAT_LIMIT = 3

EventKind = Literal[
    "reasoning",
    "content_delta",
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
        continue_check: Callable[[int], bool] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._registry = registry
        self._tool_ctx = tool_context
        # 中断检查：返回 True 表示用户请求中断。默认从不中断。
        self._interrupt_check = interrupt_check
        # 用尽轮数时的续跑检查：给已用轮数，返回 True 表示再放一批。None=不续（run 模式）。
        self._continue_check = continue_check
        # M8a：工具 schema 每轮随消息发给模型、占真实窗口。由 loop（编排层，持 registry）
        # 算好 token 估算注入 context，让 context 保持被动、不反依赖 registry。
        # 工具集在一次运行内固定，构造时算一次即可。
        tools_tokens = estimate_tools_tokens(registry.schemas())
        # system_prompt：非空则用它（如注入技能元数据）；None 时 Conversation 自建。
        self._conversation = Conversation(
            max_history_messages=config.agent.max_history_messages,
            max_context_tokens=config.agent.max_context_tokens,
            interactive=interactive,
            system_prompt=system_prompt,
            tools_tokens=tools_tokens,
            reserved_output_tokens=config.agent.reserved_output_tokens,
        )

    def _interrupted(self) -> bool:
        return self._interrupt_check is not None and self._interrupt_check()

    def set_client(self, client: LLMClient) -> None:
        """替换模型客户端（用于对话中切换 provider/模型）。

        仅换客户端，_conversation 不动——切换后对话历史完整保留。
        不改 run() 控制流。
        """
        self._client = client

    def context_report(self) -> dict[str, int]:
        """当前上下文预算分项（供 /context 展示真实占用，含 tools schema）。"""
        return self._conversation.budget_report()

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
        previous_budget = self._tool_ctx.budget
        self._tool_ctx.budget = ToolBudget(
            max_calls=self._config.agent.max_tool_calls,
            max_total_output_chars=self._config.agent.max_total_tool_output_chars,
        )
        try:
            yield from self._run_task(task)
        finally:
            self._tool_ctx.budget = previous_budget

    def _run_task(self, task: str) -> Iterator[StepEvent]:
        """执行已安装任务预算的循环主体。"""
        self._conversation.add_user(task)
        tool_schemas = self._registry.schemas()

        # 重复动作熔断：连续多轮完全相同的工具调用 → 判定卡死。
        last_signature: str | None = None
        repeat_count = 0

        # 轮数预算：用尽时若有 continue_check（交互）且用户同意，则再加一批。
        count = 0
        budget = self._config.agent.max_iterations

        while True:
            if count >= budget:
                if self._continue_check is not None and self._continue_check(count):
                    budget += self._config.agent.max_iterations
                else:
                    yield StepEvent(
                        kind="error",
                        text=(
                            f"已达最大轮数（{count}），任务未完成。已执行的步骤见上方；"
                            "可用 --max-iterations 提高上限，或在 chat 中继续对话。"
                        ),
                        is_error=True,
                    )
                    return
            count += 1
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

            # 重复动作熔断：连续 REPEAT_LIMIT 轮完全相同的工具调用 → 判定卡死，
            # 在写入 tool_calls 消息前终止（不留悬空调用）。
            signature = json.dumps(
                [(c.name, c.arguments) for c in tool_calls],
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            if signature == last_signature:
                repeat_count += 1
            else:
                last_signature = signature
                repeat_count = 1
            if repeat_count >= _REPEAT_LIMIT:
                yield StepEvent(
                    kind="error",
                    text=f"检测到连续 {repeat_count} 次相同的工具调用，已停止以避免死循环。",
                    is_error=True,
                )
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
            exhausted_reason: str | None = None
            skipped_calls = 0
            for call in tool_calls:
                yield StepEvent(
                    kind="tool_call",
                    tool_name=call.name,
                    tool_args=call.arguments,
                )
                result = self._registry.execute(call.name, call.arguments, self._tool_ctx)
                if result.budget_exhausted is not None:
                    exhausted_reason = exhausted_reason or result.budget_exhausted
                if not result.executed:
                    skipped_calls += 1
                self._conversation.add_tool_result(call.id, call.name, result.output)
                yield StepEvent(
                    kind="tool_result",
                    tool_name=call.name,
                    text=result.output,
                    is_error=result.is_error,
                )

            if exhausted_reason is not None:
                task_budget = self._tool_ctx.budget
                if task_budget is not None:
                    if exhausted_reason == "max_tool_calls":
                        limit = task_budget.max_calls
                        used = task_budget.used_calls
                    else:
                        limit = task_budget.max_total_output_chars
                        used = task_budget.used_output_chars
                    self._tool_ctx.logger.budget_exhausted(
                        reason=exhausted_reason,
                        limit=limit,
                        used=used,
                        skipped_calls=skipped_calls,
                    )
                yield StepEvent(
                    kind="error",
                    text=(
                        "任务工具预算已耗尽，已停止后续执行。"
                        "请缩小任务范围，或在配置中提高对应的 agent 预算。"
                    ),
                    is_error=True,
                )
                return
