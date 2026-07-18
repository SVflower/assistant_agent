"""上下文摘要压缩（M8b）。

长会话逼近预算时，把最旧一段完整用户轮压成摘要，替代硬丢——保留早期决策/约束/结论。
Compactor 持有 client（rank 1），由 loop（rank 3 编排）调用；context 保持被动、不依赖本模块。
关闭时 loop 根本不调用，上下文行为逐字节等于硬截断现状。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from assistant_agent.providers.ports import ModelProviderPort

from assistant_agent.agent.context.window import truncate_text_to_tokens

_SUMMARY_PROMPT = (
    "把以下对话历史压成简短要点，务必保留：关键决策、用户约束/偏好、已完成结论、"
    "未决待办。只输出要点本身，不要寒暄或解释。"
)


def group_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按完整用户轮分组：一轮 = user 消息 + 其引发的 assistant/tool 往返，直到下一个 user。

    开头非 user 的消息（罕见，如残留 tool）并入第一轮，保证不丢消息。
    """
    turns: list[list[dict[str, Any]]] = []
    for msg in messages:
        if msg.get("role") == "user" or not turns:
            turns.append([msg])
        else:
            turns[-1].append(msg)
    return turns


@dataclass
class CompactionResult:
    """一次压缩的产物，供 loop 写回 conversation 并上报 usage。"""

    summary: str
    covered_upto: int  # 已压缩到的 raw 历史游标（该下标之前的消息被摘要覆盖）
    usage: dict[str, int]  # 摘要 LLM 调用的 token 用量（计入全局 usage，不进 M6.5）


class Compactor:
    """把最旧若干完整用户轮摘要化。持有 client；摘要调用禁用工具、失败则返回 None。"""

    def __init__(
        self,
        client: ModelProviderPort,
        keep_recent_turns: int = 4,
        summary_max_tokens: int = 512,
    ) -> None:
        self._client = client
        self._keep = keep_recent_turns
        self._summary_max_tokens = summary_max_tokens

    def set_client(self, client: ModelProviderPort) -> None:
        """更新摘要 client；仅由跟随主模型的 Loop 调用。"""
        self._client = client

    def compact(
        self, tail: list[dict[str, Any]], base_covered: int, prev_summary: str = ""
    ) -> CompactionResult | None:
        """压缩 tail 里保护窗之前的完整轮。

        tail：checkpoint 之后的未压缩消息（raw[base_covered:]）。
        base_covered：tail 在 raw 历史中的起始下标。
        prev_summary：已有摘要，会与新内容合并成累积摘要。
        返回 None 表示无可压缩或摘要失败——loop 据此回退硬截断，会话不中断。
        """
        turns = group_turns(tail)
        if len(turns) <= self._keep:
            return None  # 没有超出保护窗的轮，不压
        to_compact = turns[: len(turns) - self._keep]
        msgs = [m for turn in to_compact for m in turn]
        if not msgs:
            return None
        covered_upto = base_covered + len(msgs)

        summary, usage = self._summarize(prev_summary, msgs)
        if not summary:
            return None  # 摘要失败/空 → 降级
        return CompactionResult(summary=summary, covered_upto=covered_upto, usage=usage)

    def _summarize(
        self, prev_summary: str, msgs: list[dict[str, Any]]
    ) -> tuple[str, dict[str, int]]:
        """调模型把 msgs（含已有摘要）压成新摘要。禁用工具。异常→空串（降级信号）。"""
        transcript = _render(msgs)
        if prev_summary:
            transcript = f"【已有摘要】\n{prev_summary}\n\n【新增对话】\n{transcript}"
        request = [
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": transcript},
        ]
        parts: list[str] = []
        usage: dict[str, int] = {}
        try:
            for event in self._client.complete_stream(messages=request, tools=None):
                if event.kind == "content":
                    parts.append(event.text)
                elif event.kind == "usage":
                    usage = event.usage
                elif event.kind == "error":
                    return "", {}
        except Exception:  # noqa: BLE001 - 摘要是尽力而为，任何异常都降级为硬截断
            return "", {}
        summary = truncate_text_to_tokens(
            "".join(parts).strip(), self._summary_max_tokens, overhead=0
        )
        return summary, usage


def _render(messages: list[dict[str, Any]]) -> str:
    """把消息列表渲染成给摘要模型看的纯文本。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = str(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            content += f"[调用 {fn.get('name')}({fn.get('arguments', '')})]"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
