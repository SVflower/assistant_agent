"""ReAct 主循环测试。用假流式 LLMClient 驱动，无需真实模型。"""

from __future__ import annotations

from collections.abc import Iterator

from assistant_agent.agent.failures import ContinuationResult
from assistant_agent.agent.loop import AgentLoop
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import StreamEvent, ToolCall
from assistant_agent.obs import NullLogger
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.registry import ToolRegistry, build_default_registry


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


class _SpyLogger(NullLogger):
    def __init__(self) -> None:
        self.budget_events: list[dict] = []

    def budget_exhausted(self, *, reason: str, limit: int, used: int, skipped_calls: int) -> None:
        self.budget_events.append(
            {
                "reason": reason,
                "limit": limit,
                "used": used,
                "skipped_calls": skipped_calls,
            }
        )


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


def _tools_round(*calls: ToolCall) -> list[StreamEvent]:
    return [StreamEvent(kind="tool_calls", tool_calls=list(calls))]


def _config(
    max_iterations: int = 25,
    *,
    max_tool_calls: int = 50,
    max_total_tool_output_chars: int = 50_000,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "active": "test",
            "providers": {"test": {"model": "openai/fake"}},
            "agent": {
                "max_iterations": max_iterations,
                "max_tool_calls": max_tool_calls,
                "max_total_tool_output_chars": max_total_tool_output_chars,
            },
        }
    )


def _loop(client, config=None, interrupt_check=None) -> AgentLoop:
    return AgentLoop(
        config or _config(),
        client,  # 鸭子类型：只需有 complete_stream 方法
        build_default_registry(),
        ToolContext(confirm=lambda _m: "allow"),
        interrupt_check=interrupt_check,
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
    # 模型只调工具、不收尾 → 应在 max_iterations 后报错终止。
    # 每轮用不同 args，避免触发重复动作熔断（那是另一条路径）。
    rounds = [
        _tool_round(ToolCall(id="x", name="list_dir", arguments={"path": f"dir{i}"}))
        for i in range(10)
    ]
    client = FakeStreamClient(rounds)
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
    assert events[-1].text == "模型调用失败。"
    assert events[-1].failure is not None
    assert events[-1].failure.code == "internal_error"


def test_loop_two_rounds_share_history():
    """连续两次 run 复用同一对话（同会话记忆）。"""
    loop = _loop(FakeStreamClient([_text_round("第一次"), _text_round("第二次")]))
    events1 = list(loop.run("第一个问题"))
    events2 = list(loop.run("第二个问题"))
    assert events1[-1].text == "第一次"
    assert events2[-1].text == "第二次"


def test_loop_interrupt_during_stream_preserves_content():
    """流式中途中断：保留已输出正文，干净终止为 interrupted。"""
    client = FakeStreamClient([_text_round("正在输出的内容")])
    # 中断检查始终为真：第一个事件后即触发中断
    events = list(_loop(client, interrupt_check=lambda: True).run("test"))
    # 已输出的 content_delta 仍在
    deltas = [e.text for e in events if e.kind == "content_delta"]
    assert "".join(deltas)  # 至少输出了部分
    assert events[-1].kind == "interrupted"


def test_loop_interrupt_before_tool_batch():
    """工具批次执行前中断：不执行工具，干净终止。"""
    call = ToolCall(id="c1", name="list_dir", arguments={"path": "."})
    client = FakeStreamClient([_tool_round(call)])
    # 中断触发：流结束后、工具执行前应停止
    events = list(_loop(client, interrupt_check=lambda: True).run("test"))
    # 没有工具真正执行（无 tool_result）
    assert not [e for e in events if e.kind == "tool_result"]
    assert events[-1].kind == "interrupted"


def test_loop_not_interrupted_when_check_false():
    """中断检查为假时，正常完成，不受影响。"""
    client = FakeStreamClient([_text_round("正常完成")])
    events = list(_loop(client, interrupt_check=lambda: False).run("test"))
    assert events[-1].kind == "final"
    assert events[-1].text == "正常完成"


def test_loop_continue_check_extends_budget():
    """用尽轮数时 continue_check 返回 True → 再放一批继续。"""
    # 每轮不同工具调用（避免重复熔断），第 3 轮才收尾
    rounds = [
        _tool_round(ToolCall(id="x", name="list_dir", arguments={"path": f"d{i}"}))
        for i in range(2)
    ] + [_text_round("终于完成")]
    client = FakeStreamClient(rounds)
    calls = {"n": 0}

    def cont(_used: int) -> bool:
        calls["n"] += 1
        return True  # 每次都同意续

    # max_iterations=2：跑完 2 轮未完成 → 问续 → 加批 → 第 3 轮收尾
    loop = AgentLoop(
        _config(max_iterations=2),
        client,
        build_default_registry(),
        ToolContext(confirm=lambda _m: "allow"),
        continue_check=cont,
    )
    events = list(loop.run("test"))
    assert events[-1].kind == "final"
    assert events[-1].text == "终于完成"
    assert calls["n"] >= 1  # 至少问过一次是否继续


def test_loop_no_continue_check_stops_gracefully():
    """无 continue_check（run 模式）：用尽轮数优雅终止，提示如何继续。"""
    rounds = [
        _tool_round(ToolCall(id="x", name="list_dir", arguments={"path": f"d{i}"}))
        for i in range(10)
    ]
    client = FakeStreamClient(rounds)
    events = list(_loop(client, _config(max_iterations=2)).run("test"))
    assert events[-1].kind == "error"
    assert "已达最大轮数" in events[-1].text
    assert "max-iterations" in events[-1].text  # 提示如何继续


def test_loop_circuit_breaks_on_repeated_action():
    """连续相同工具调用达阈值 → 熔断终止，不空耗到最大轮数。"""
    same = _tool_round(ToolCall(id="x", name="list_dir", arguments={"path": "."}))
    # 提供远超阈值的相同轮次；应在第 3 次相同时熔断
    client = FakeStreamClient([same] * 10)
    events = list(_loop(client, _config(max_iterations=20)).run("卡死"))
    assert events[-1].kind == "error"
    assert "死循环" in events[-1].text
    # 熔断发生在第 3 轮，远早于 max_iterations=20
    assert client.calls == 3


def test_set_client_preserves_history():
    """对话中切换 client：历史完整保留（切模型不丢上下文）。"""
    loop = _loop(FakeStreamClient([_text_round("第一次回答")]))
    list(loop.run("第一个问题"))
    before = loop.export_history()

    # 换一个新 client 继续对话
    new_client = FakeStreamClient([_text_round("第二次回答")])
    loop.set_client(new_client)
    list(loop.run("第二个问题"))
    after = loop.export_history()

    # 切换后历史包含切换前的全部内容（前缀一致），且用了新 client
    assert after[: len(before)] == before
    assert new_client.calls == 1
    assert any("第一个问题" in str(m.get("content")) for m in after)
    assert any("第二个问题" in str(m.get("content")) for m in after)


def test_oversized_task_stops_before_client_call():
    config = AppConfig.model_validate(
        {
            "active": "test",
            "providers": {"test": {"model": "openai/fake"}},
            "agent": {"max_context_tokens": 100, "reserved_output_tokens": 0},
        }
    )
    client = FakeStreamClient([_text_round("不应调用")])
    loop = AgentLoop(config, client, ToolRegistry(), ToolContext(), system_prompt="SYS")

    events = list(loop.run("x" * 1000))

    assert events[-1].kind == "error"
    assert "用户输入过长" in events[-1].text
    assert client.calls == 0


def test_set_client_updates_default_compactor_client():
    config = AppConfig.model_validate(
        {
            "active": "test",
            "providers": {"test": {"model": "openai/fake"}},
            "agent": {"compaction": {"enabled": True}},
        }
    )
    original = FakeStreamClient([_text_round("old")])
    replacement = FakeStreamClient([_text_round("new")])
    loop = AgentLoop(config, original, ToolRegistry(), ToolContext(), system_prompt="SYS")

    loop.set_client(replacement)

    assert loop._compactor is not None
    assert loop._compactor._client is replacement


def test_set_client_keeps_explicit_summary_provider(monkeypatch):
    fixed_summary_client = FakeStreamClient([_text_round("summary")])
    monkeypatch.setattr(
        "assistant_agent.agent.loop.LLMClient", lambda _provider: fixed_summary_client
    )
    config = AppConfig.model_validate(
        {
            "active": "main",
            "providers": {
                "main": {"model": "openai/main"},
                "summary": {"model": "openai/summary"},
            },
            "agent": {
                "compaction": {"enabled": True, "summary_model": "summary"},
            },
        }
    )
    loop = AgentLoop(
        config,
        FakeStreamClient([_text_round("main")]),
        ToolRegistry(),
        ToolContext(),
        system_prompt="SYS",
    )

    loop.set_client(FakeStreamClient([_text_round("replacement")]))

    assert loop._compactor is not None
    assert loop._compactor._client is fixed_summary_client


def test_tool_call_budget_completes_batch_without_orphans():
    calls = [
        ToolCall(id=f"c{i}", name="list_dir", arguments={"path": f"missing-{i}"}) for i in range(3)
    ]
    client = FakeStreamClient([_tools_round(*calls)])
    loop = _loop(client, _config(max_tool_calls=2))

    events = list(loop.run("批量调用"))

    results = [event for event in events if event.kind == "tool_result"]
    assert len(results) == 3
    assert results[-1].is_error
    assert "工具调用预算已耗尽" in results[-1].text
    assert events[-1].kind == "error"
    assert client.calls == 1

    history = loop.export_history()
    assistant_calls = next(message["tool_calls"] for message in history if "tool_calls" in message)
    tool_messages = [message for message in history if message["role"] == "tool"]
    assert len(assistant_calls) == len(tool_messages) == 3
    assert {call["id"] for call in assistant_calls} == {
        message["tool_call_id"] for message in tool_messages
    }


def test_tool_call_budget_emits_audit_event():
    calls = [
        ToolCall(id=f"c{i}", name="list_dir", arguments={"path": f"missing-{i}"}) for i in range(3)
    ]
    logger = _SpyLogger()
    loop = AgentLoop(
        _config(max_tool_calls=1),
        FakeStreamClient([_tools_round(*calls)]),
        build_default_registry(),
        ToolContext(logger=logger),
    )

    list(loop.run("批量调用"))

    assert logger.budget_events == [
        {"reason": "max_tool_calls", "limit": 1, "used": 1, "skipped_calls": 2}
    ]


def test_tool_call_budget_accumulates_across_rounds():
    rounds = [
        _tool_round(ToolCall(id=f"c{i}", name="list_dir", arguments={"path": f"d{i}"}))
        for i in range(3)
    ]
    client = FakeStreamClient(rounds)
    events = list(_loop(client, _config(max_tool_calls=2)).run("跨轮调用"))

    assert events[-1].kind == "error"
    assert "工具调用预算已耗尽" in [e.text for e in events if e.kind == "tool_result"][-1]
    assert client.calls == 3


class _FixedOutputTool(Tool):
    name = "fixed_output"
    description = "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("x" * 100)

    def permission_requests(self, args, ctx):
        return []


class _ControlTool(Tool):
    description = "test"

    def __init__(self, name: str, action: str, calls: list[str]) -> None:
        self.name = name
        self.action = action
        self.calls = calls

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def run(self, args, ctx) -> ToolResult:
        self.calls.append(self.name)
        if self.action == "pause":
            ctx.run_control.request_pause()
        elif self.action == "cancel":
            ctx.run_control.request_cancel()
        return ToolResult.ok("done")

    def permission_requests(self, args, ctx):
        return []


def test_pause_inside_batch_stops_before_next_tool():
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(_ControlTool("first", "pause", calls))
    registry.register(_ControlTool("second", "", calls))
    client = FakeStreamClient(
        [_tools_round(ToolCall("c1", "first", {}), ToolCall("c2", "second", {}))]
    )
    loop = AgentLoop(_config(), client, registry, ToolContext())

    events = list(loop.run("pause"))

    assert calls == ["first"]
    assert events[-1].kind == "interrupted"
    assert len([item for item in loop.export_history() if item["role"] == "tool"]) == 1


def test_cancel_inside_batch_resolves_remaining_tool_calls():
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(_ControlTool("first", "cancel", calls))
    registry.register(_ControlTool("second", "", calls))
    client = FakeStreamClient(
        [_tools_round(ToolCall("c1", "first", {}), ToolCall("c2", "second", {}))]
    )
    loop = AgentLoop(_config(), client, registry, ToolContext())

    events = list(loop.run("cancel"))

    assert calls == ["first"]
    assert events[-1].kind == "interrupted"
    tool_messages = [item for item in loop.export_history() if item["role"] == "tool"]
    assert len(tool_messages) == 2
    assert "未执行" in tool_messages[-1]["content"]


def test_total_output_budget_truncates_then_stops():
    registry = ToolRegistry()
    registry.register(_FixedOutputTool())
    client = FakeStreamClient([_tool_round(ToolCall(id="c1", name="fixed_output", arguments={}))])
    ctx = ToolContext(max_output_chars=1000)
    loop = AgentLoop(
        _config(max_total_tool_output_chars=40),
        client,
        registry,
        ctx,
    )

    events = list(loop.run("大输出"))

    result = next(event for event in events if event.kind == "tool_result")
    assert len(result.text) == 40
    assert "输出已截断" in result.text
    assert events[-1].kind == "error"
    assert client.calls == 1
    assert ctx.budget is None


def test_total_output_budget_completes_remaining_batch_results():
    registry = ToolRegistry()
    registry.register(_FixedOutputTool())
    client = FakeStreamClient(
        [
            _tools_round(
                ToolCall(id="c1", name="fixed_output", arguments={}),
                ToolCall(id="c2", name="fixed_output", arguments={}),
            )
        ]
    )
    loop = AgentLoop(
        _config(max_total_tool_output_chars=40),
        client,
        registry,
        ToolContext(max_output_chars=1000),
    )

    events = list(loop.run("批量大输出"))

    results = [event for event in events if event.kind == "tool_result"]
    assert len(results) == 2
    assert len(results[0].text) == 40
    assert "累计工具输出预算已耗尽" in results[1].text
    history = loop.export_history()
    assert len([message for message in history if message["role"] == "tool"]) == 2


def test_budget_resets_for_each_run():
    rounds = [
        _tool_round(ToolCall(id="c1", name="list_dir", arguments={"path": "a"})),
        _text_round("第一轮完成"),
        _tool_round(ToolCall(id="c2", name="list_dir", arguments={"path": "b"})),
        _text_round("第二轮完成"),
    ]
    client = FakeStreamClient(rounds)
    loop = _loop(client, _config(max_tool_calls=1))

    first = list(loop.run("任务一"))
    second = list(loop.run("任务二"))

    assert first[-1].kind == "final"
    assert second[-1].kind == "final"
    assert client.calls == 4


def test_continue_iterations_does_not_reset_tool_budget():
    rounds = [
        _tool_round(ToolCall(id="c1", name="list_dir", arguments={"path": "a"})),
        _tool_round(ToolCall(id="c2", name="list_dir", arguments={"path": "b"})),
    ]
    client = FakeStreamClient(rounds)
    loop = AgentLoop(
        _config(max_iterations=1, max_tool_calls=1),
        client,
        build_default_registry(),
        ToolContext(),
        continue_check=lambda _used: True,
    )

    events = list(loop.run("继续但不扩工具预算"))

    assert events[-1].kind == "error"
    assert client.calls == 2


def test_tool_call_budget_continuation_extends_current_run():
    calls = [
        ToolCall(id="c1", name="list_dir", arguments={"path": "a"}),
        ToolCall(id="c2", name="list_dir", arguments={"path": "b"}),
    ]
    prompts = []
    client = FakeStreamClient([_tools_round(*calls), _text_round("done")])
    loop = AgentLoop(
        _config(max_tool_calls=1),
        client,
        build_default_registry(),
        ToolContext(),
        budget_continue_check=lambda prompt: (
            prompts.append(prompt) or ContinuationResult("continue-calls", True)
        ),
    )

    events = list(loop.run("continue calls"))

    assert events[-1].kind == "final"
    assert len([event for event in events if event.kind == "tool_result"]) == 2
    assert [(item.resource, item.used, item.limit) for item in prompts] == [("tool_calls", 1, 1)]


def test_tool_output_budget_continuation_allows_next_round():
    registry = ToolRegistry()
    registry.register(_FixedOutputTool())
    prompts = []
    client = FakeStreamClient(
        [_tool_round(ToolCall("c1", "fixed_output", {})), _text_round("done")]
    )
    loop = AgentLoop(
        _config(max_total_tool_output_chars=40),
        client,
        registry,
        ToolContext(max_output_chars=1000),
        budget_continue_check=lambda prompt: (
            prompts.append(prompt) or ContinuationResult("continue-output", True)
        ),
    )

    events = list(loop.run("continue output"))

    assert events[-1].kind == "final"
    assert [item.resource for item in prompts] == ["tool_output"]


def test_output_extension_inside_batch_does_not_prompt_twice():
    registry = ToolRegistry()
    registry.register(_FixedOutputTool())
    prompts = []
    client = FakeStreamClient(
        [
            _tools_round(
                ToolCall("c1", "fixed_output", {}),
                ToolCall("c2", "fixed_output", {}),
            ),
            _text_round("done"),
        ]
    )
    loop = AgentLoop(
        _config(max_total_tool_output_chars=40),
        client,
        registry,
        ToolContext(max_output_chars=1000),
        budget_continue_check=lambda prompt: (
            prompts.append(prompt) or ContinuationResult("continue-output", True)
        ),
    )

    events = list(loop.run("batch output"))

    assert events[-1].kind == "final"
    assert [item.resource for item in prompts] == ["tool_output"]


def test_continuation_hard_extension_count_stops_run():
    config = AppConfig.model_validate(
        {
            "active": "test",
            "providers": {"test": {"model": "openai/fake"}},
            "agent": {
                "max_iterations": 1,
                "continuation": {
                    "max_extensions": 1,
                    "iteration_increment": 1,
                    "max_iterations_hard": 2,
                },
            },
        }
    )
    rounds = [
        _tool_round(ToolCall("c1", "list_dir", {"path": "a"})),
        _tool_round(ToolCall("c2", "list_dir", {"path": "b"})),
    ]
    loop = AgentLoop(
        config,
        FakeStreamClient(rounds),
        build_default_registry(),
        ToolContext(),
        budget_continue_check=lambda _prompt: ContinuationResult("once", True),
    )

    events = list(loop.run("bounded"))

    assert events[-1].failure is not None
    assert events[-1].failure.code == "iteration_limit_reached"
