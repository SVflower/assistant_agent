"""对话上下文（截断 + 序列化）测试。"""

from __future__ import annotations

import pytest

from assistant_agent.agent.context import Conversation
from assistant_agent.agent.token_budget import ContextWindowError


def _conv(**kwargs) -> Conversation:
    # 固定 system_prompt，避免动态环境影响 token 预算断言
    kwargs.setdefault("system_prompt", "SYS")
    return Conversation(**kwargs)


def test_export_load_roundtrip():
    c = _conv()
    c.add_user("问题一")
    c.add_assistant("回答一")
    hist = c.export_history()
    assert hist == [
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "回答一"},
    ]

    c2 = _conv()
    c2.load_history(hist)
    assert c2.export_history() == hist


def test_export_excludes_system():
    c = _conv(system_prompt="我是系统提示")
    c.add_user("hi")
    hist = c.export_history()
    # 导出不含 system
    assert all(m["role"] != "system" for m in hist)


def test_messages_always_starts_with_system():
    c = _conv(system_prompt="SYS")
    c.add_user("hi")
    msgs = c.messages()
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "SYS"


def test_token_budget_truncates_oldest():
    # 预算很小：system(≈7) + 只能容下最近少量消息
    c = _conv(max_context_tokens=40)
    for _ in range(20):
        c.add_user("x" * 20)  # 每条约 24 token
    msgs = c.messages()
    body = msgs[1:]  # 去掉 system
    # 预算约束下只保留最近的少数几条，不是全部 20 条
    assert 0 < len(body) < 20
    # 保留的是最新的（尾部）
    assert body[-1]["content"] == "x" * 20


def test_truncation_drops_orphan_tool_message():
    c = _conv(max_context_tokens=60)
    # 构造：assistant(tool_calls) + tool + 之后一堆长消息把前面挤出预算
    c.add_assistant("调用", tool_calls=[{"id": "1", "function": {"name": "f", "arguments": "{}"}}])
    c.add_tool_result("1", "f", "结果")
    for _ in range(10):
        c.add_user("y" * 30)
    body = c.messages()[1:]
    # 截断后开头不应是孤立的 tool 消息
    assert not (body and body[0].get("role") == "tool")


def test_message_count_hard_cap():
    c = _conv(max_history_messages=5, max_context_tokens=10_000_000)
    for i in range(20):
        c.add_user(f"m{i}")
    body = c.messages()[1:]
    # token 预算极大，但消息数硬上限 5 仍生效
    assert len(body) <= 5


# ---- M8a：预算口径（tools schema + reserved_output）----
def _schema(n_chars: int) -> dict:
    """造一个序列化后约 n_chars 的假工具 schema。"""
    return {"type": "function", "function": {"name": "t", "description": "d" * n_chars}}


def test_estimate_tools_tokens_empty_is_zero():
    from assistant_agent.agent.context import estimate_tools_tokens

    assert estimate_tools_tokens([]) == 0  # 无工具→0，保证回归口径


def test_estimate_tools_tokens_grows_with_schemas():
    from assistant_agent.agent.context import estimate_tools_tokens

    small = estimate_tools_tokens([_schema(10)])
    big = estimate_tools_tokens([_schema(10), _schema(500)])
    assert big > small > 0


def test_tools_tokens_shrink_message_budget():
    # 同样消息，注入 tools_tokens 后应更早截断（保留更少）
    def build(tools_tokens: int) -> int:
        c = _conv(max_context_tokens=200, tools_tokens=tools_tokens)
        for _ in range(30):
            c.add_user("z" * 10)
        return len(c.messages()[1:])

    base = build(0)
    with_tools = build(120)
    assert with_tools < base  # 工具占预算→消息保留更少


def test_reserved_output_shrinks_budget():
    def build(reserved: int) -> int:
        c = _conv(max_context_tokens=200, reserved_output_tokens=reserved)
        for _ in range(30):
            c.add_user("z" * 10)
        return len(c.messages()[1:])

    assert build(120) < build(0)  # 预留回复→消息保留更少


def test_budget_report_sums_and_defaults_zero():
    c = _conv(max_context_tokens=8000, tools_tokens=300, reserved_output_tokens=1024)
    c.add_user("hello")
    r = c.budget_report()
    assert r["total"] == 8000 and r["tools"] == 300 and r["reserved"] == 1024
    assert r["used"] == r["system"] + r["tools"] + r["reserved"] + r["messages"]


def test_default_overhead_zero_is_regression_safe():
    # 不传 tools_tokens/reserved 时，budget_report 的 tools/reserved 为 0（口径等于旧行为）
    c = _conv(max_context_tokens=8000)
    c.add_user("hi")
    r = c.budget_report()
    assert r["tools"] == 0 and r["reserved"] == 0


def test_huge_latest_user_message_is_rejected_before_provider():
    c = _conv(max_context_tokens=100)
    with pytest.raises(ContextWindowError, match="用户输入过长"):
        c.add_user("x" * 1000)


def test_final_envelope_never_exceeds_window_with_huge_summary():
    c = _conv(max_context_tokens=100)
    c.add_user("recent")
    c.set_checkpoint("巨" * 1000, covered_upto=0)
    messages = c.messages()
    report = c.budget_report()
    assert report["used"] <= report["total"] == 100
    assert any(message.get("content") == "recent" for message in messages)


def test_huge_tool_result_keeps_complete_protocol_block():
    c = _conv(max_context_tokens=120)
    c.add_assistant(
        None,
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ],
    )
    c.add_tool_result("c1", "read", "x" * 1000)

    body = c.messages()[1:]

    assert [message["role"] for message in body] == ["assistant", "tool"]
    assert len(body[1]["content"]) < 1000
    assert c.budget_report()["used"] <= 120


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"summary": "ok", "covered_upto": 2},
        {"summary": 1, "covered_upto": 0},
        {"summary": "ok", "covered_upto": "0"},
    ],
)
def test_invalid_checkpoint_is_rejected(checkpoint):
    c = _conv()
    c.add_user("hi")
    with pytest.raises(ValueError, match="checkpoint"):
        c.load_checkpoint(checkpoint)
