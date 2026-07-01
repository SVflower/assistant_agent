"""ReAct 主循环测试。用假流式 LLMClient 驱动，无需真实模型。"""

from __future__ import annotations

from collections.abc import Iterator

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import StreamEvent, ToolCall
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import build_default_registry


class FakeStreamClient:
    """按预设脚本逐轮 yield StreamEvent 序列，模拟流式模型的多轮决策。

    每一轮是一个 StreamEvent 列表；complete_stream 每被调用一次消费一轮。
    """

    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
        self._rounds = rounds
        self.calls = 0

    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        events = self._rounds[self.calls]
        self.calls += 1
        yield from events


def _text_round(text: str) -> list[StreamEvent]:
    """一轮纯文本回复（拆成两个 content 增量，验证拼接）。"""
    mid = len(text) // 2
    return [
        StreamEvent(kind="content", text=text[:mid]),
        StreamEvent(kind="content", text=text[mid:]),
    ]


def _tool_round(call: ToolCall) -> list[StreamEvent]:
    """一轮工具调用（客户端已拼接好，一次性给出）。"""
    return [StreamEvent(kind="tool_calls", tool_calls=[call])]


def _config(max_iterations: int = 25) -> AppConfig:
    return AppConfig.model_validate(
        {
            "active": "test",
            "providers": {"test": {"model": "openai/fake"}},
            "agent": {"max_iterations": max_iterations},
        }
    )


def _loop(client, config=None) -> AgentLoop:
    return AgentLoop(
        config or _config(),
        client,  # 鸭子类型：只需有 complete_stream 方法
        build_default_registry(),
        ToolContext(confirm=lambda _m: True),
    )


def test_loop_finishes_without_tools():
    client = FakeStreamClient([_text_round("任务完成了。")])
    events = list(_loop(client).run("随便做点什么"))
    assert events[-1].kind == "final"
    assert events[-1].text == "任务完成了。"
    assert client.calls == 1


def test_loop_streams_content_deltas():
    """正文以 content_delta 增量事件流出，最终 final 是完整拼接。"""
    client = FakeStreamClient([_text_round("你好世界")])
    events = list(_loop(client).run("打个招呼"))
    deltas = [e.text for e in events if e.kind == "content_delta"]
    assert "".join(deltas) == "你好世界"
    assert events[-1].kind == "final"
    assert events[-1].text == "你好世界"


def test_loop_executes_tool_then_finishes(tmp_path):
    target = tmp_path / "out.txt"
    client = FakeStreamClient(
        [
            _tool_round(
                ToolCall(
                    id="c1",
                    name="write_file",
                    arguments={"path": str(target), "content": "data"},
                )
            ),
            _text_round("已写入文件。"),
        ]
    )
    events = list(_loop(client).run("写入文件"))
    kinds = [e.kind for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert events[-1].kind == "final"
    assert target.read_text(encoding="utf-8") == "data"
    assert client.calls == 2


def test_loop_respects_max_iterations():
    # 模型永远只调工具，不收尾 → 应在 max_iterations 后报错终止
    looping = _tool_round(ToolCall(id="x", name="list_dir", arguments={"path": "."}))
    client = FakeStreamClient([looping] * 10)
    events = list(_loop(client, _config(max_iterations=3)).run("无限循环"))
    assert events[-1].kind == "error"
    assert "最大轮数" in events[-1].text
    assert client.calls == 3


def test_loop_handles_unknown_tool():
    client = FakeStreamClient(
        [
            _tool_round(ToolCall(id="c1", name="bogus", arguments={})),
            _text_round("抱歉，那个工具不存在。"),
        ]
    )
    events = list(_loop(client).run("调用不存在的工具"))
    tool_results = [e for e in events if e.kind == "tool_result"]
    assert tool_results
    assert tool_results[0].is_error
    assert events[-1].kind == "final"


def test_loop_forwards_usage():
    """usage 事件被转发给 UI 层。"""
    client = FakeStreamClient(
        [
            [
                StreamEvent(kind="content", text="好的"),
                StreamEvent(kind="usage", usage={"total_tokens": 42}),
            ]
        ]
    )
    events = list(_loop(client).run("test"))
    usage_events = [e for e in events if e.kind == "usage"]
    assert usage_events
    assert usage_events[0].usage == {"total_tokens": 42}


def test_loop_forwards_reasoning():
    """reasoning 增量被转发给 UI 层。"""
    client = FakeStreamClient(
        [[StreamEvent(kind="reasoning", text="让我想想"), *_text_round("答案是42")]]
    )
    events = list(_loop(client).run("test"))
    reasoning = [e for e in events if e.kind == "reasoning"]
    assert reasoning
    assert reasoning[0].text == "让我想想"


def test_loop_stream_error_preserves_content():
    """流中途出错：已输出内容保留，本轮标记错误终止。"""
    client = FakeStreamClient(
        [
            [
                StreamEvent(kind="content", text="已经说了一半"),
                StreamEvent(kind="error", text="连接中断"),
            ]
        ]
    )
    events = list(_loop(client).run("test"))
    # 已输出的 content_delta 仍在
    deltas = [e.text for e in events if e.kind == "content_delta"]
    assert "".join(deltas) == "已经说了一半"
    # 最终是错误事件
    assert events[-1].kind == "error"
    assert "连接中断" in events[-1].text


def test_loop_two_rounds_share_history():
    """连续两次 run 复用同一对话（同会话记忆）。"""
    loop = _loop(FakeStreamClient([_text_round("第一次"), _text_round("第二次")]))
    events1 = list(loop.run("第一个问题"))
    events2 = list(loop.run("第二个问题"))
    assert events1[-1].text == "第一次"
    assert events2[-1].text == "第二次"
