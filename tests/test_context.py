"""对话上下文（截断 + 序列化）测试。"""

from __future__ import annotations

from assistant_agent.agent.context import Conversation


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
