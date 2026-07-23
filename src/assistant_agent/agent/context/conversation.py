"""对话上下文管理（上下文工程 + 序列化）。

两个职责：
1. 上下文工程：累积历史，按 token 预算截断——保留 system(头) + 最近消息(尾)，
   契合模型"lost-in-the-middle"的注意力规律（见 docs/archive/phase1/m3-memory-plan.md）。
2. 序列化：导出/载入原始历史（不含 system），供会话持久化。

注意这里的 Conversation 是“模型工作上下文”，不是公开 Session ledger。压缩或最终硬截断可以改变
发给 provider 的消息，但不能反向删改用户可见的长期会话事实。
"""

from __future__ import annotations

from typing import Any

from assistant_agent.agent.context.window import (
    DEFAULT_ESTIMATOR,
    ContextWindowError,
    TokenEstimator,
    estimate_message_tokens,
    truncate_text_to_tokens,
)
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.contracts.attachments import (
    MessageContentV1,
    UserMessageInputV1,
    attachment_token_estimate,
    parse_message_content,
)


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    """向后兼容的默认估算入口。"""
    return estimate_message_tokens(message)


def estimate_tools_tokens(schemas: list[dict[str, Any]]) -> int:
    """估算工具 schema 发给模型占的 token（同字符/4→字符近似口径，保守）。

    工具 schema 每轮随 messages 一起发给模型、占真实窗口。M8a 前预算不计它（D10），
    MCP 接入大量工具后会显著偏低。序列化成紧凑 JSON 后按字符数近似（与消息同口径）。
    空列表返回 0——保证"无工具"时预算与旧口径一致。
    """
    from assistant_agent.agent.context.window import estimate_tools_tokens as estimate

    return estimate(schemas)


class Conversation:
    """维护 OpenAI 格式的消息历史。

    system 消息始终保留在最前，截断只作用于其后的对话消息。

    `_messages` 保留原始工作历史，`messages()` 才按当前预算生成 provider-facing 视图。这种“双历史”
    设计让 checkpoint 可以恢复，同时避免为了某次模型窗口裁剪而永久丢掉原始内容。
    """

    def __init__(
        self,
        max_history_messages: int = 40,
        max_context_tokens: int = 8000,
        system_prompt: str | None = None,
        interactive: bool = True,
        tools_tokens: int = 0,
        reserved_output_tokens: int = 0,
        estimator: TokenEstimator = DEFAULT_ESTIMATOR,
        attachment_context_limit: int = 0,
        image_token_reserve: int = 2048,
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
        self._estimator = estimator
        self._attachment_context_limit = attachment_context_limit
        self._image_token_reserve = image_token_reserve
        # M8b：摘要 checkpoint。None（默认）时下方全部逻辑等于 M8b 前——不压缩、硬截断。
        # {"summary": 摘要文本, "covered_upto": 已覆盖到的 _messages 游标}
        self._checkpoint: dict[str, Any] | None = None
        fixed = self._fixed_tokens()
        if fixed + 5 > self._max_tokens:
            raise ContextWindowError(
                "上下文窗口过小：system、tools schema 与 reserved_output 已无消息空间"
            )

    def add_user(self, content: str | UserMessageInputV1 | MessageContentV1) -> None:
        if isinstance(content, str):
            parsed = UserMessageInputV1.from_text(content).content
        elif isinstance(content, UserMessageInputV1):
            parsed = content.content
        else:
            parsed = content
        attachment_tokens = attachment_token_estimate(
            parsed, image_reserve=self._image_token_reserve
        )
        if self._attachment_context_limit and attachment_tokens > self._attachment_context_limit:
            raise ContextWindowError("附件上下文成本超过当前模型可用消息预算，请减少附件或缩小内容")
        message = {"role": "user", "content": parsed.model_dump(mode="json")}
        if self._message_tokens(message) > self._message_budget():
            raise ContextWindowError(
                f"用户输入过长，无法放入 {self._max_tokens} token 的上下文窗口；请缩短输入"
            )
        self._messages.append(message)

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
        # 每次调用都重新计算视图而不是修改 `_messages`。因此调整预算或恢复 checkpoint 后，
        # 不会把一次临时截断误当成已经持久化的真实历史。
        if self._checkpoint is None:
            result = [self._system, *self._truncated(self._messages)]
        else:
            summary_msg: dict[str, Any] | None = self._summary_message()
            tail = self._messages[self._checkpoint["covered_upto"] :]
            # 最新用户输入优先于摘要。摘要过长时先裁摘要，必要时本轮省略摘要，
            # 不能让合法的新任务被 checkpoint 挤出请求。
            if summary_msg is not None and tail and tail[-1].get("role") == "user":
                summary_budget = self._message_budget() - self._message_tokens(tail[-1])
                if self._message_tokens(summary_msg) > summary_budget:
                    summary_msg = self._fit_message(summary_msg, summary_budget)
            if summary_msg is None:
                result = [self._system, *self._truncated(tail)]
                used = (
                    self._tools_tokens
                    + self._reserved_output_tokens
                    + sum(self._message_tokens(message) for message in result)
                )
                if used > self._max_tokens:
                    raise ContextWindowError(
                        f"上下文封套超限：估算 {used} tokens，配置窗口 {self._max_tokens}"
                    )
                return result
            extra = self._message_tokens(summary_msg)
            result = [self._system, summary_msg, *self._truncated(tail, extra_overhead=extra)]
        used = (
            self._tools_tokens
            + self._reserved_output_tokens
            + sum(self._message_tokens(message) for message in result)
        )
        if used > self._max_tokens:
            raise ContextWindowError(
                f"上下文封套超限：估算 {used} tokens，配置窗口 {self._max_tokens}"
            )
        return result

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
        system = self._message_tokens(self._system)
        body = sent[1:]
        messages = sum(self._message_tokens(m) for m in body)
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
            self._message_tokens(self._system) + self._tools_tokens + self._reserved_output_tokens
        )
        if self._checkpoint is None:
            active = self._messages
        else:
            fixed += self._message_tokens(self._summary_message())
            active = self._messages[self._checkpoint["covered_upto"] :]
        return fixed + sum(self._message_tokens(m) for m in active)

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
        if not isinstance(covered_upto, int) or not 0 <= covered_upto <= len(self._messages):
            raise ValueError("摘要 checkpoint 的 covered_upto 越界")
        available = self._message_budget()
        rendered_prefix = "[早前对话摘要，供你参考上下文]\n"
        max_summary_tokens = max(0, available - len(rendered_prefix) - 4)
        safe_summary = truncate_text_to_tokens(summary, max_summary_tokens, overhead=0)
        self._checkpoint = {"summary": safe_summary, "covered_upto": covered_upto}

    def get_checkpoint(self) -> dict[str, Any] | None:
        """导出 checkpoint（供 Session 持久化）。"""
        return dict(self._checkpoint) if self._checkpoint else None

    def load_checkpoint(self, checkpoint: dict[str, Any] | None) -> None:
        """载入 checkpoint（供 resume 恢复，避免重复摘要）。"""
        if checkpoint is None:
            self._checkpoint = None
            return
        if not isinstance(checkpoint, dict):
            raise ValueError("摘要 checkpoint 必须是对象")
        summary = checkpoint.get("summary")
        covered_upto = checkpoint.get("covered_upto")
        if not isinstance(summary, str):
            raise ValueError("摘要 checkpoint 缺少字符串 summary")
        if not isinstance(covered_upto, int):
            raise ValueError("摘要 checkpoint 缺少整数 covered_upto")
        self.set_checkpoint(summary, covered_upto)

    # ---- 序列化：供会话持久化（不含 system，system 运行时按当前环境重建）----

    def export_history(self) -> list[dict[str, Any]]:
        """导出原始对话历史（完整、未截断、不含 system），用于存档。"""
        return [dict(m) for m in self._messages]

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        """载入历史（替换当前对话），用于恢复会话。"""
        loaded: list[dict[str, Any]] = []
        for message in messages:
            copied = dict(message)
            if copied.get("role") == "user":
                copied["content"] = parse_message_content(copied.get("content")).model_dump(
                    mode="json"
                )
            loaded.append(copied)
        self._messages = loaded

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
            - self._message_tokens(self._system)
            - self._tools_tokens
            - self._reserved_output_tokens
            - extra_overhead
        )
        blocks = self._message_blocks(candidates)
        kept_blocks: list[list[dict[str, Any]]] = []
        used = 0
        # 从最新往最旧按协议块遍历，assistant tool_calls 与其全部 tool 结果不可拆开。
        for block in reversed(blocks):
            cost = sum(self._message_tokens(msg) for msg in block)
            if used + cost > budget:
                if not kept_blocks:
                    fitted = self._fit_block(block, budget)
                    if fitted is not None:
                        kept_blocks.append(fitted)
                    elif block[0].get("tool_calls"):
                        raise ContextWindowError(
                            "工具调用参数过长，无法在上下文窗口内保留完整调用协议"
                        )
                break
            kept_blocks.append(block)
            used += cost
        kept_blocks.reverse()
        kept = [message for block in kept_blocks for message in block]

        # 丢弃开头孤立的 tool 消息（其对应的 assistant 调用可能被截掉）
        while kept and kept[0].get("role") == "tool":
            kept = kept[1:]
        return kept

    def _message_tokens(self, message: dict[str, Any]) -> int:
        return estimate_message_tokens(message, self._estimator)

    def _fixed_tokens(self) -> int:
        return (
            self._message_tokens(self._system) + self._tools_tokens + self._reserved_output_tokens
        )

    def _message_budget(self) -> int:
        return max(0, self._max_tokens - self._fixed_tokens())

    def _fit_message(self, message: dict[str, Any], budget: int) -> dict[str, Any] | None:
        """只裁剪非用户消息内容；工具调用参数不可安全裁剪，装不下则丢弃。"""
        if budget < 4 or message.get("tool_calls"):
            return None
        fitted = dict(message)
        fitted["content"] = truncate_text_to_tokens(str(message.get("content") or ""), budget)
        return fitted if self._message_tokens(fitted) <= budget else None

    @staticmethod
    def _message_blocks(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """把 assistant tool_calls 与紧随其后的全部 tool 结果组成不可拆协议块。"""
        blocks: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(messages):
            block = [messages[index]]
            if messages[index].get("tool_calls"):
                index += 1
                while index < len(messages) and messages[index].get("role") == "tool":
                    block.append(messages[index])
                    index += 1
                blocks.append(block)
                continue
            blocks.append(block)
            index += 1
        return blocks

    def _fit_block(self, block: list[dict[str, Any]], budget: int) -> list[dict[str, Any]] | None:
        """裁剪最新非用户块；工具批次保留调用与全部结果，只缩结果正文。"""
        if not block or block[0].get("role") == "user":
            return None
        if not block[0].get("tool_calls"):
            fitted = self._fit_message(block[0], budget)
            return [fitted] if fitted is not None else None

        fitted_block = [dict(message) for message in block]
        tool_results = fitted_block[1:]
        for message in tool_results:
            message["content"] = ""
        fixed = sum(self._message_tokens(message) for message in fitted_block)
        if fixed > budget:
            return None
        remaining = budget - fixed
        for index, message in enumerate(tool_results):
            slots = len(tool_results) - index
            share = remaining // slots
            original = str(block[index + 1].get("content") or "")
            message["content"] = truncate_text_to_tokens(original, share, overhead=0)
            remaining -= len(message["content"])
        return fitted_block
