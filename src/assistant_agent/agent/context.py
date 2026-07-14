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


def estimate_tools_tokens(schemas: list[dict[str, Any]]) -> int:
    """估算工具 schema 发给模型占的 token（同字符/4→字符近似口径，保守）。

    工具 schema 每轮随 messages 一起发给模型、占真实窗口。M8a 前预算不计它（D10），
    MCP 接入大量工具后会显著偏低。序列化成紧凑 JSON 后按字符数近似（与消息同口径）。
    空列表返回 0——保证"无工具"时预算与旧口径一致。
    """
    if not schemas:
        return 0
    import json

    return len(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")))


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
        tools_tokens: int = 0,
        reserved_output_tokens: int = 0,
    ) -> None:
        prompt = system_prompt if system_prompt is not None else build_system_prompt(interactive)
        self._system: dict[str, Any] = {"role": "system", "content": prompt}
        self._messages: list[dict[str, Any]] = []
        self._max = max_history_messages
        self._max_tokens = max_context_tokens
        # M8a：工具 schema 与预留回复也占窗口，从消息预算里扣掉。
        # 两者默认 0 → 预算口径与 M8a 前逐字节一致（回归保护）。
        self._tools_tokens = tools_tokens
        self._reserved_output_tokens = reserved_output_tokens
        # M8b：摘要 checkpoint。None（默认）时下方全部逻辑等于 M8b 前——不压缩、硬截断。
        # {"summary": 摘要文本, "covered_upto": 已覆盖到的 _messages 游标}
        self._checkpoint: dict[str, Any] | None = None

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
        """返回发给模型的消息列表。

        无 checkpoint（默认）：[system, *截断后的历史]——与 M8b 前逐字节一致。
        有 checkpoint：[system, 摘要消息, *截断后的尾段]，摘要顶替已覆盖的旧历史。
        """
        if self._checkpoint is None:
            return [self._system, *self._truncated(self._messages)]
        summary_msg = self._summary_message()
        tail = self._messages[self._checkpoint["covered_upto"] :]
        # 摘要消息也占预算，从尾段可用预算里扣掉。
        extra = _estimate_message_tokens(summary_msg)
        return [self._system, summary_msg, *self._truncated(tail, extra_overhead=extra)]

    def _summary_message(self) -> dict[str, Any]:
        """把 checkpoint 摘要渲染成一条 user 消息（跨模型最兼容，不破坏 system-first）。"""
        text = self._checkpoint["summary"] if self._checkpoint else ""
        return {"role": "user", "content": f"[早前对话摘要，供你参考上下文]\n{text}"}

    def budget_report(self) -> dict[str, int]:
        """当前上下文预算分项（供 /context 展示真实占用）。

        system/tools/reserved 为固定开销，messages 为截断后实际保留的估算 token，
        used=三者+messages，total=配置窗口。M8a 前 tools/reserved 恒为 0。
        """
        sent = self.messages()  # system (+summary?) + 截断后消息
        system = _estimate_message_tokens(self._system)
        body = sent[1:]
        messages = sum(_estimate_message_tokens(m) for m in body)
        return {
            "total": self._max_tokens,
            "system": system,
            "tools": self._tools_tokens,
            "reserved": self._reserved_output_tokens,
            "messages": messages,
            "used": system + self._tools_tokens + self._reserved_output_tokens + messages,
            "compacted": 1 if self._checkpoint else 0,
        }

    # ---- M8b：摘要 checkpoint 存取 + 阈值判断 ----

    def full_usage(self) -> int:
        """未截断历史 + 固定开销的总 token 估算（供压缩阈值判断，不受截断影响）。"""
        fixed = (
            _estimate_message_tokens(self._system)
            + self._tools_tokens
            + self._reserved_output_tokens
        )
        if self._checkpoint is None:
            active = self._messages
        else:
            fixed += _estimate_message_tokens(self._summary_message())
            active = self._messages[self._checkpoint["covered_upto"] :]
        return fixed + sum(_estimate_message_tokens(m) for m in active)

    def budget(self) -> int:
        """当前上下文总预算（供 loop 算阈值）。"""
        return self._max_tokens

    def tail_after_checkpoint(self) -> tuple[list[dict[str, Any]], int, str]:
        """返回 (checkpoint 之后的未压缩消息, 起始游标, 已有摘要)，供 Compactor。"""
        if self._checkpoint is None:
            return list(self._messages), 0, ""
        upto = self._checkpoint["covered_upto"]
        return self._messages[upto:], upto, self._checkpoint["summary"]

    def set_checkpoint(self, summary: str, covered_upto: int) -> None:
        """写入/更新摘要 checkpoint（loop 压缩后调用）。"""
        self._checkpoint = {"summary": summary, "covered_upto": covered_upto}

    def get_checkpoint(self) -> dict[str, Any] | None:
        """导出 checkpoint（供 Session 持久化）。"""
        return dict(self._checkpoint) if self._checkpoint else None

    def load_checkpoint(self, checkpoint: dict[str, Any] | None) -> None:
        """载入 checkpoint（供 resume 恢复，避免重复摘要）。"""
        self._checkpoint = dict(checkpoint) if checkpoint else None

    # ---- 序列化：供会话持久化（不含 system，system 运行时按当前环境重建）----

    def export_history(self) -> list[dict[str, Any]]:
        """导出原始对话历史（完整、未截断、不含 system），用于存档。"""
        return [dict(m) for m in self._messages]

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        """载入历史（替换当前对话），用于恢复会话。"""
        self._messages = [dict(m) for m in messages]

    # ---- 截断（上下文工程）----

    def _truncated(
        self, source: list[dict[str, Any]], extra_overhead: int = 0
    ) -> list[dict[str, Any]]:
        """token 感知截断：在消息数硬上限内，保留能装进 token 预算的最近消息。

        - system 的 token 计入预算（它始终在最前）。
        - 从最新往旧累加，装不下就停——保留"尾部"最近上下文。
        - 不切断 assistant↔tool 配对：丢弃开头孤立的 tool 消息。
        - extra_overhead：额外固定开销（如摘要消息 token），从预算里扣掉；默认 0。
        """
        # 先套用消息数硬上限（兜底，防极端条数）
        candidates = source[-self._max :] if len(source) > self._max else list(source)

        # 消息可用预算 = 总窗口 − system − tools schema − 预留回复 − 额外开销（后三者默认 0）。
        budget = (
            self._max_tokens
            - _estimate_message_tokens(self._system)
            - self._tools_tokens
            - self._reserved_output_tokens
            - extra_overhead
        )
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
