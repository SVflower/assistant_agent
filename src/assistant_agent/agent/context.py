"""对话上下文管理（上下文工程 + 序列化）。

两个职责：
1. 上下文工程：累积历史，按 token 预算截断——保留 system(头) + 最近消息(尾)，
   契合模型"lost-in-the-middle"的注意力规律（见 docs/archive/phase1/m3-memory-plan.md）。
2. 序列化：导出/载入原始历史（不含 system），供会话持久化。
"""

from __future__ import annotations

from typing import Any

from assistant_agent.agent.prompts import build_system_prompt


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    """估算单条消息的 token 数（保守、快速、无重依赖）。

    用字符数近似（约 1 token/字符）。理由：
    - 我们的提示词/内容多含中文，CJK 约 1~2 token/字符——按 1 token/字符是安全下界偏保守；
    - 对英文会高估（英文约 4 字符/token），但高估只会多丢历史、绝不会撑爆窗口——截断场景宁可保守；
    - 不在热路径引入 litellm.token_counter：避免每轮的模型查找开销与偶发慢/失败。
    """
    text = str(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        text += str(call.get("function", {}).get("arguments", ""))
    return len(text) + 4  # +4 约等于每条消息的角色/分隔开销


class Conversation:
    """维护 OpenAI 格式的消息历史。

    system 消息始终保留在最前，截断只作用于其后的对话消息。
    """

    def __init__(
        self,
        max_history_messages: int = 40,
        max_context_tokens: int = 8000,
        system_prompt: str | None = None,
        interactive: bool = True,
    ) -> None:
        prompt = system_prompt if system_prompt is not None else build_system_prompt(interactive)
        self._system: dict[str, Any] = {"role": "system", "content": prompt}
        self._messages: list[dict[str, Any]] = []
        self._max = max_history_messages
        self._max_tokens = max_context_tokens

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

    # ---- 序列化：供会话持久化（不含 system，system 运行时按当前环境重建）----

    def export_history(self) -> list[dict[str, Any]]:
        """导出原始对话历史（完整、未截断、不含 system），用于存档。"""
        return [dict(m) for m in self._messages]

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        """载入历史（替换当前对话），用于恢复会话。"""
        self._messages = [dict(m) for m in messages]

    # ---- 截断（上下文工程）----

    def _truncated(self) -> list[dict[str, Any]]:
        """token 感知截断：在消息数硬上限内，保留能装进 token 预算的最近消息。

        - system 的 token 计入预算（它始终在最前）。
        - 从最新往旧累加，装不下就停——保留"尾部"最近上下文。
        - 不切断 assistant↔tool 配对：丢弃开头孤立的 tool 消息。
        """
        # 先套用消息数硬上限（兜底，防极端条数）
        candidates = self._messages[-self._max :] if len(self._messages) > self._max else list(
            self._messages
        )

        budget = self._max_tokens - _estimate_message_tokens(self._system)
        kept: list[dict[str, Any]] = []
        used = 0
        # 从最新往最旧遍历，能装下就保留
        for msg in reversed(candidates):
            cost = _estimate_message_tokens(msg)
            if kept and used + cost > budget:
                break
            kept.append(msg)
            used += cost
        kept.reverse()

        # 丢弃开头孤立的 tool 消息（其对应的 assistant 调用可能被截掉）
        while kept and kept[0].get("role") == "tool":
            kept = kept[1:]
        return kept
