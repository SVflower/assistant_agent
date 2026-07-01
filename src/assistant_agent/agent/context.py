"""对话上下文管理。

负责累积消息历史，并在超长时做长度感知截断——
本地模型上下文窗口比云端小得多，必须主动控制。
"""

from __future__ import annotations

from typing import Any

from assistant_agent.agent.prompts import build_system_prompt


class Conversation:
    """维护 OpenAI 格式的消息历史。

    system 消息始终保留在最前，截断只作用于其后的对话消息。
    """

    def __init__(self, max_history_messages: int = 40, system_prompt: str | None = None) -> None:
        prompt = system_prompt if system_prompt is not None else build_system_prompt()
        self._system: dict[str, Any] = {"role": "system", "content": prompt}
        self._messages: list[dict[str, Any]] = []
        self._max = max_history_messages

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(
        self,
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": content,
            }
        )

    def messages(self) -> list[dict[str, Any]]:
        """返回发给模型的完整消息列表（system + 截断后的历史）。"""
        return [self._system, *self._truncated()]

    def _truncated(self) -> list[dict[str, Any]]:
        """长度感知截断：保留最近的消息，但不切断 assistant↔tool 的配对。"""
        if len(self._messages) <= self._max:
            return self._messages
        tail = self._messages[-self._max :]
        # 若截断点恰好把 tool 消息留在最前（其对应的 assistant 调用被切掉），
        # 模型会报错。向前丢弃这些孤立的 tool 消息。
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]
        return tail
