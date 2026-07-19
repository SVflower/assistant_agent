"""M15 terminal activity feedback and decision visibility."""

from __future__ import annotations

import pytest
from rich.console import Console as RichConsole

from assistant_agent.contracts.events import StepEvent
from assistant_agent.tools.display import call_display
from assistant_agent.ui.activity import ActivityController, ActivityIndicator
from assistant_agent.ui.console import Console
from assistant_agent.ui.conversation_renderer import ConversationRenderer
from assistant_agent.ui.tool_renderer import ToolRenderer


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _Live:
    instances = []

    def __init__(self, renderable, **kwargs) -> None:
        self.renderable = renderable
        self.kwargs = kwargs
        self.starts = 0
        self.stops = 0
        self.instances.append(self)

    def start(self, refresh=False) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


def _render(renderable) -> str:
    console = RichConsole(record=True, width=100)
    console.print(renderable)
    return console.export_text()


def test_indicator_timer_advances_without_new_agent_event():
    clock = _Clock()
    indicator = ActivityIndicator(clock=clock)
    indicator.update("正在编辑", "src/app.py")
    assert "0.0s" in _render(indicator)

    clock.value = 9.25
    output = _render(indicator)
    assert "9.2s" in output
    assert "仍在等待" in output and "Ctrl+C 可暂停" in output


def test_indicator_resets_timer_only_when_phase_changes():
    clock = _Clock()
    indicator = ActivityIndicator(clock=clock)
    clock.value = 9.0
    indicator.update("分析任务")
    assert "0.0s" in _render(indicator)
    assert "仍在等待" not in _render(indicator)

    clock.value = 10.0
    indicator.update("分析任务")
    assert "1.0s" in _render(indicator)


def test_controller_reuses_one_live_and_updates_phase_in_place():
    _Live.instances.clear()
    bound = []
    console = RichConsole(record=True, width=100)
    controller = ActivityController(
        console,
        enabled=True,
        on_live=bound.append,
        live_factory=_Live,
    )
    controller.show("等待模型响应")
    controller.show("分析任务")
    controller.show("分析任务")

    live = _Live.instances[0]
    assert len(_Live.instances) == 1
    assert live.starts == 1
    assert live.kwargs["refresh_per_second"] == 8
    assert bound == [live]

    controller.suspend()
    controller.resume("执行已授权操作")
    controller.complete()
    assert live.starts == 2 and live.stops == 2
    assert bound == [live, None, live, None]


def test_controller_disables_live_for_dumb_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    console = RichConsole(record=True, force_terminal=True, width=100)
    controller = ActivityController(console)
    controller.show("等待模型响应")
    assert controller.enabled is False
    assert controller.active is False
    monkeypatch.delenv("TERM", raising=False)


def test_confirmation_resumes_activity_only_when_allowed(monkeypatch):
    class Activity:
        def __init__(self) -> None:
            self.events = []

        def suspend(self):
            self.events.append("suspend")

        def resume(self, text):
            self.events.append(("resume", text))

    console = Console()
    console._console = RichConsole(record=True, width=80)
    activity = Activity()
    console._activity = activity
    monkeypatch.setattr(console, "input", lambda _prompt: "1")
    assert console.confirm("需要授权") == "allow"
    assert activity.events == ["suspend", ("resume", "执行已授权操作")]

    activity.events.clear()
    monkeypatch.setattr(console, "input", lambda _prompt: "3")
    assert console.confirm("需要授权") == "deny"
    assert activity.events == ["suspend"]


def test_scoped_confirmation_resumes_for_tool_and_server_grants(monkeypatch):
    class Activity:
        def __init__(self) -> None:
            self.events = []

        def suspend(self):
            self.events.append("suspend")

        def resume(self, text):
            self.events.append(("resume", text))

    console = Console()
    console._console = RichConsole(record=True, width=80)
    activity = Activity()
    console._activity = activity

    for answer, expected in (("1", "allow"), ("2", "always"), ("3", "broader")):
        activity.events.clear()
        monkeypatch.setattr(console, "input", lambda _prompt, value=answer: value)
        assert console.confirm_scoped("需要授权", "信任 server") == expected
        assert activity.events == ["suspend", ("resume", "执行已授权操作")]


def test_continue_confirmation_resumes_activity_only_when_accepted(monkeypatch):
    class Activity:
        def __init__(self) -> None:
            self.events = []

        def suspend(self):
            self.events.append("suspend")

        def resume(self, text):
            self.events.append(("resume", text))

    console = Console()
    activity = Activity()
    console._activity = activity
    monkeypatch.setattr(console, "input", lambda _prompt: "y")
    assert console.confirm_continue(10) is True
    assert activity.events == ["suspend", ("resume", "继续处理")]

    activity.events.clear()
    monkeypatch.setattr(console, "input", lambda _prompt: "n")
    assert console.confirm_continue(10) is False
    assert activity.events == ["suspend"]


def test_normal_only_persists_external_decisions_and_change_previews():
    console = RichConsole(record=True, width=100)
    renderer = ToolRenderer(console, "normal")
    read = call_display("read_file", {"path": "notes.txt"})
    shell = call_display("run_shell", {"command": "pytest -q"})
    write = call_display("write_file", {"path": "out.txt", "content": "ok"})

    renderer.call(StepEvent(kind="tool_call", tool_name="read_file", display=read))
    renderer.call(StepEvent(kind="tool_call", tool_name="run_shell", display=shell))
    renderer.call(StepEvent(kind="tool_call", tool_name="write_file", display=write))
    output = console.export_text()

    assert "读取 notes.txt" not in output
    assert "◆ 准备运行命令 pytest -q" in output
    assert "准备写入 · 1 行" in output


def test_display_importance_is_conservative_for_extensions():
    assert call_display("read_file", {"path": "a"}).importance == "routine"
    assert call_display("edit_file", {"path": "a"}).importance == "change"
    assert call_display("mcp__demo__write", {}).importance == "external"
    assert call_display("unknown_extension", {}).importance == "external"


class _Owner:
    def __init__(self) -> None:
        self._console = RichConsole(record=True, width=100)
        self._active_live = None
        self._activity = None
        self._at_line_start = True
        self._model_label = "test-model"
        self._context_limit = 8000


class _ActivitySpy:
    instances = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.enabled = _kwargs.get("enabled")
        self.events = []
        self.complete_calls = 0
        self.instances.append(self)

    def show(self, action, target="") -> None:
        self.events.append(("show", action, target))

    def suspend(self) -> None:
        self.events.append(("suspend",))

    def complete(self) -> None:
        self.complete_calls += 1


def test_renderer_tracks_tool_and_notice_phases_and_cleans_up(monkeypatch):
    _ActivitySpy.instances.clear()
    monkeypatch.setattr("assistant_agent.ui.conversation_renderer.ActivityController", _ActivitySpy)
    owner = _Owner()
    args = {"command": "pytest -q"}
    events = iter(
        [
            StepEvent(kind="reasoning", text="hidden"),
            StepEvent(
                kind="tool_call",
                tool_name="run_shell",
                tool_args=args,
                display=call_display("run_shell", args),
            ),
            StepEvent(kind="tool_result", tool_name="run_shell", text="ok"),
            StepEvent(kind="notice", text="上下文已压缩"),
            StepEvent(kind="final", text="完成"),
        ]
    )

    ConversationRenderer(owner, "normal", False).render(events)

    activity = _ActivitySpy.instances[0]
    assert ("show", "等待模型响应", "") in activity.events
    assert ("show", "分析任务", "") in activity.events
    assert ("show", "正在运行命令 pytest -q", "") in activity.events
    assert ("show", "评估下一步", "") in activity.events
    assert ("show", "继续处理", "") in activity.events
    assert activity.complete_calls >= 1
    assert owner._activity is None


@pytest.mark.parametrize("terminal_kind", ["final", "error", "interrupted"])
def test_renderer_terminal_events_clean_up_activity(monkeypatch, terminal_kind):
    _ActivitySpy.instances.clear()
    monkeypatch.setattr("assistant_agent.ui.conversation_renderer.ActivityController", _ActivitySpy)
    owner = _Owner()

    ConversationRenderer(owner, "normal", False).render(
        iter([StepEvent(kind=terminal_kind, text="done")])
    )

    activity = _ActivitySpy.instances[0]
    assert activity.complete_calls >= 1
    assert owner._activity is None


def test_renderer_quiet_disables_activity_and_exception_still_cleans_up(monkeypatch):
    _ActivitySpy.instances.clear()
    monkeypatch.setattr("assistant_agent.ui.conversation_renderer.ActivityController", _ActivitySpy)
    owner = _Owner()

    def broken_events():
        yield StepEvent(kind="reasoning", text="hidden")
        raise RuntimeError("stream failed")

    try:
        ConversationRenderer(owner, "quiet", False).render(broken_events())
    except RuntimeError as exc:
        assert str(exc) == "stream failed"
    else:
        raise AssertionError("renderer should propagate stream failures")

    activity = _ActivitySpy.instances[0]
    assert activity.enabled is False
    assert activity.complete_calls == 1
    assert owner._activity is None
