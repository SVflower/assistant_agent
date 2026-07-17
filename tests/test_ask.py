"""ask_user（层1 意图澄清）工具测试。"""

from __future__ import annotations

from assistant_agent.interaction import QuestionAnswer, SafeDefaultInteractionPort
from assistant_agent.tools.ask import AskUserTool
from assistant_agent.tools.base import NO_USER_AVAILABLE, ToolContext


def _ctx(ask=None, *, interactive=True) -> ToolContext:
    return ToolContext(ask=ask or (lambda _q, _o: ""), interactive=interactive)


def test_ask_interactive_returns_user_choice():
    captured = {}

    def fake_ask(question, options):
        captured["q"] = question
        captured["o"] = options
        return "方案A"

    r = AskUserTool().run(
        {"question": "选哪个方案？", "options": ["方案A", "方案B"]}, _ctx(fake_ask)
    )
    assert not r.is_error
    assert "方案A" in r.output
    assert captured["q"] == "选哪个方案？"
    assert captured["o"] == ["方案A", "方案B"]


def test_ask_non_interactive_degrades():
    # 非交互 Runtime：不阻塞、不调 ask，直接退化。
    called = {"n": 0}

    def should_not_call(q, o):
        called["n"] += 1
        return "x"

    r = AskUserTool().run(
        {"question": "q", "options": ["a", "b"]},
        _ctx(should_not_call, interactive=False),
    )
    assert not r.is_error
    assert r.output == NO_USER_AVAILABLE
    assert called["n"] == 0  # 非交互不调用 ask，不阻塞自动化


def test_ask_missing_question():
    r = AskUserTool().run({"options": ["a"]}, _ctx())
    assert r.is_error


def test_ask_missing_options():
    r = AskUserTool().run({"question": "q"}, _ctx())
    assert r.is_error


def test_ask_empty_options():
    r = AskUserTool().run({"question": "q", "options": []}, _ctx())
    assert r.is_error


def test_ask_uses_service_interaction_without_tty() -> None:
    class Port(SafeDefaultInteractionPort):
        request = None

        def ask_question(self, request):
            self.request = request
            return QuestionAnswer(request.request_id, answer="方案B", available=True)

    port = Port()
    context = ToolContext(interaction=port, interactive=True, current_call_id="call-1")
    context.bind_run("run-1", "session-1")
    result = AskUserTool().run({"question": "选哪个？", "options": ["方案A", "方案B"]}, context)
    assert result.output == "用户选择：方案B"
    assert port.request is not None
    assert (port.request.run_id, port.request.session_id, port.request.call_id) == (
        "run-1",
        "session-1",
        "call-1",
    )
