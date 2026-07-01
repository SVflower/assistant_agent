"""ReAct 主循环测试。用假 LLMClient 驱动，无需真实模型。"""

from __future__ import annotations

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMResponse, ToolCall
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import build_default_registry


class FakeClient:
    """按预设脚本逐次返回 LLMResponse，模拟模型的多轮决策。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    def complete(self, messages, tools=None) -> LLMResponse:
        response = self._responses[self.calls]
        self.calls += 1
        return response


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
        client,  # 鸭子类型：只需有 complete 方法
        build_default_registry(),
        ToolContext(confirm=lambda _m: True),
    )


def test_loop_finishes_without_tools():
    client = FakeClient([LLMResponse(content="任务完成了。")])
    events = list(_loop(client).run("随便做点什么"))
    assert events[-1].kind == "final"
    assert events[-1].text == "任务完成了。"
    assert client.calls == 1


def test_loop_executes_tool_then_finishes(tmp_path):
    target = tmp_path / "out.txt"
    client = FakeClient(
        [
            # 第一轮：请求写文件
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_file",
                        arguments={"path": str(target), "content": "data"},
                    )
                ]
            ),
            # 第二轮：无工具调用 → 完成
            LLMResponse(content="已写入文件。"),
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
    looping = LLMResponse(tool_calls=[ToolCall(id="x", name="list_dir", arguments={"path": "."})])
    client = FakeClient([looping] * 10)
    events = list(_loop(client, _config(max_iterations=3)).run("无限循环"))
    assert events[-1].kind == "error"
    assert "最大轮数" in events[-1].text
    assert client.calls == 3


def test_loop_handles_unknown_tool():
    client = FakeClient(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="bogus", arguments={})]),
            LLMResponse(content="抱歉，那个工具不存在。"),
        ]
    )
    events = list(_loop(client).run("调用不存在的工具"))
    tool_results = [e for e in events if e.kind == "tool_result"]
    assert tool_results
    assert tool_results[0].is_error
    assert events[-1].kind == "final"
