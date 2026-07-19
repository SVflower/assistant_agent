"""公共 Session/Run 服务门面的端到端契约。"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from assistant_agent.bootstrap import runtime as runtime_module
from assistant_agent.interaction import (
    BlockingInteractionPort,
    ContinueDecision,
    DefinitionChangeDecision,
    RecoveryDecision,
    SafeDefaultInteractionPort,
)
from assistant_agent.persistence.store import SessionStore
from assistant_agent.providers.ports import StreamEvent, ToolCall
from assistant_agent.service import (
    AgentService,
    ArtifactNotFoundError,
    ArtifactUnavailableError,
    RuntimeConfigError,
    SessionBusyError,
    SessionRunConflictError,
)
from tests.support import ToolBudget


class _FakeClient:
    def __init__(self, _provider) -> None:
        pass

    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        yield StreamEvent(kind="content", text="done")


def _config(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.yaml"
    path.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_module, "LLMClient", _FakeClient)
    return path


def test_public_facade_runs_and_syncs_terminal_session(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session(interaction=SafeDefaultInteractionPort())
    try:
        assert "present_chart" not in session_runtime.capabilities.tools
        assert any(
            item.code == "chart_presentation_omitted_context_limit"
            for item in session_runtime.runtime.notices
        )
        execution = session_runtime.start_run("task")
        events = list(execution.events)
        assert any(item.kind == "final" and item.text == "done" for item in events)
        assert events[-1].kind == "run_terminal"
        assert events[-1].terminal_status == "completed"
        kinds = [item.kind for item in events]
        assert kinds.index("final") < kinds.index("run_terminal")
        assert kinds.count("run_terminal") == 1
        assert kinds[-2:] == ["activity", "run_terminal"]
        assert events[-2].phase == "syncing_session"

        saved = session_runtime.runtime.session_store.load(session_runtime.session.id)
        assert saved.messages[-1] == {"role": "assistant", "content": "done"}
        run = session_runtime.runtime.run_store.load(execution.run_id).document
        assert run["status"] == "completed"
        assert run["session_synced"] is True
    finally:
        session_runtime.close()


class _ChartClient:
    def __init__(self, _provider) -> None:
        self.calls = 0

    def complete_stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        "chart-call",
                        "present_chart",
                        {
                            "schema_version": 1,
                            "chart_type": "bar",
                            "title": "数量",
                            "columns": [
                                {
                                    "key": "name",
                                    "label": "名称",
                                    "data_type": "string",
                                    "unit": None,
                                },
                                {
                                    "key": "value",
                                    "label": "数量",
                                    "data_type": "number",
                                    "unit": None,
                                },
                            ],
                            "rows": [["A", 1], ["B", 2]],
                            "x_key": "name",
                            "series": [{"key": "value", "label": "数量"}],
                            "category_key": None,
                            "value_key": None,
                        },
                    )
                ],
            )
        else:
            yield StreamEvent(kind="content", text="图表已生成")


def test_chart_event_history_snapshot_and_cascade_delete(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    config.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 16000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_module, "LLMClient", _ChartClient)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    session_runtime = service.create_session()
    session_id = session_runtime.session.id
    execution = session_runtime.start_run("画图")
    events = list(execution.events)
    chart_event = next(item for item in events if item.kind == "tool_result")
    assert chart_event.chart is not None
    assert [item.kind for item in events][-3:] == ["final", "activity", "run_terminal"]
    artifact_id = chart_event.chart.artifact_id
    assert service.get_artifact(session_id, artifact_id) == chart_event.chart
    assert session_runtime.get_artifact(artifact_id) == chart_event.chart
    assert session_runtime.list_presentations() == (chart_event.chart,)
    snapshot = session_runtime.snapshot()
    assert snapshot.artifacts == (chart_event.chart.ref,)
    assert snapshot.assistant_messages[-1].id == chart_event.chart.message_id
    assert snapshot.assistant_messages[-1].artifacts == (chart_event.chart.ref,)
    run_snapshot = session_runtime.run_snapshot(execution.run_id)
    assert run_snapshot.artifacts == (chart_event.chart.ref,)
    session_runtime.close()

    assert service.delete_session(session_id) is True
    assert all(item.id != execution.run_id for item in service.list_runs())
    with pytest.raises(ArtifactNotFoundError):
        service.get_artifact(session_id, artifact_id)


def test_corrupt_artifact_state_is_typed_unavailable(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    session_id = session_runtime.session.id
    session_runtime.close()
    store = service._session_store
    store._path(session_id).write_text("{broken", encoding="utf-8")
    with pytest.raises(ArtifactUnavailableError) as caught:
        service.get_artifact(session_id, "chart_" + "0" * 24)
    assert "broken" not in str(caught.value)


def test_same_session_rejects_second_active_run(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        first = session_runtime.start_run("first")
        with pytest.raises(SessionBusyError):
            session_runtime.start_run("second")
        assert list(first.events)[-1].terminal_status == "completed"
    finally:
        session_runtime.close()


def test_two_session_runtimes_are_isolated(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    second = service.create_session()
    try:
        assert first.runtime.loop is not second.runtime.loop
        assert first.runtime.run_control is not second.runtime.run_control
        assert (
            first.runtime.tool_context.permission_grants
            is not second.runtime.tool_context.permission_grants
        )
        first.runtime.tool_context.always_allowed.add("demo")
        assert "demo" not in second.runtime.tool_context.always_allowed

        first_run = first.start_run("first")
        second_run = second.start_run("second")
        assert list(first_run.events)[-1].terminal_status == "completed"
        assert list(second_run.events)[-1].terminal_status == "completed"
    finally:
        first.close()
        second.close()


def test_pause_and_resume_keep_original_run_id(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        execution = session_runtime.start_run("task")
        iterator = execution.events
        first = next(event for event in iterator if event.kind == "content_delta")
        assert first.text
        session_runtime.pause()
        paused = list(iterator)
        assert paused[-1].terminal_status == "paused"

        resumed = session_runtime.resume_run(execution.run_id)
        assert resumed.run_id == execution.run_id
        assert list(resumed.events)[-1].terminal_status == "completed"
    finally:
        session_runtime.close()


class _AcceptDefinitionPort(SafeDefaultInteractionPort):
    def __init__(self) -> None:
        self.request = None

    def confirm_definition_change(self, request):
        self.request = request
        return DefinitionChangeDecision(request.request_id, accepted=True)


def test_definition_change_requires_interaction_and_keeps_run_id(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    first = service.create_session()
    execution = first.start_run("task")
    iterator = execution.events
    next(iterator)
    first.pause()
    assert list(iterator)[-1].terminal_status == "paused"
    session_id = first.session.id
    first.close()

    config.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake-v2\n",
        encoding="utf-8",
    )
    port = _AcceptDefinitionPort()
    resumed_runtime = service.load_session(session_id, interaction=port)
    try:
        resumed = resumed_runtime.resume_run(execution.run_id)
        assert resumed.run_id == execution.run_id
        assert list(resumed.events)[-1].terminal_status == "completed"
        assert port.request is not None
        assert "model" in {item.field for item in port.request.differences}
    finally:
        resumed_runtime.close()


def test_cancel_wakes_definition_change_interaction_and_cancels_run(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    first = service.create_session()
    execution = first.start_run("task")
    iterator = execution.events
    next(iterator)
    first.pause()
    assert list(iterator)[-1].terminal_status == "paused"
    session_id = first.session.id
    first.close()
    config.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake-v2\n",
        encoding="utf-8",
    )
    port = BlockingInteractionPort(timeout=5)
    resumed_runtime = service.load_session(session_id, interaction=port)
    resumed = []
    worker = threading.Thread(
        target=lambda: resumed.append(resumed_runtime.resume_run(execution.run_id)), daemon=True
    )
    try:
        worker.start()
        request = port.next_request(timeout=0.5)
        assert request is not None and request.kind == "definition_change"
        resumed_runtime.cancel()
        worker.join(timeout=1)
        assert len(resumed) == 1
        events = list(resumed[0].events)
        terminals = [event for event in events if event.kind == "run_terminal"]
        assert len(terminals) == 1
        assert terminals[0].terminal_status == "cancelled"
        assert resumed[0].run_id == execution.run_id
    finally:
        resumed_runtime.close()


class _SkipRecoveryPort(SafeDefaultInteractionPort):
    def __init__(self) -> None:
        self.request = None

    def decide_recovery(self, request):
        self.request = request
        return RecoveryDecision(request.request_id, "skip")


def test_uncertain_tool_recovery_uses_interaction_port(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    coordinator = first.runtime.new_run("task", first.session.id)
    assert coordinator is not None
    messages = [{"role": "user", "content": "task"}]
    coordinator.initialize(messages, None, ToolBudget(max_calls=10))
    call = ToolCall(
        "call-1",
        "write_file",
        {"path": "x.txt", "content": "x", "api_key": "secret-value"},
    )
    planned = [
        *messages,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call.id, "type": "function", "function": {}}],
        },
    ]
    coordinator.model_completed(planned, [call])
    coordinator.tool_started(call.id, [], "requires_decision")
    run_id = coordinator.run_id
    session_id = first.session.id
    first.close()

    port = _SkipRecoveryPort()
    resumed_runtime = service.load_session(session_id, interaction=port)
    try:
        resumed = resumed_runtime.resume_run(run_id)
        assert list(resumed.events)[-1].terminal_status == "completed"
        assert port.request is not None
        assert port.request.call_id == "call-1"
        assert "重复" in port.request.duplicate_side_effect_risk
        assert "secret-value" not in port.request.display_summary
    finally:
        resumed_runtime.close()


class _ToolClient:
    def __init__(self, _provider) -> None:
        pass

    def complete_stream(self, messages, tools=None):
        yield StreamEvent(
            kind="tool_calls",
            tool_calls=[ToolCall("call-read", "read_file", {"path": "sample.txt"})],
        )


class _StopContinuePort(SafeDefaultInteractionPort):
    def __init__(self) -> None:
        self.request = None

    def confirm_continue(self, request):
        self.request = request
        return ContinueDecision(request.request_id, continue_run=False)


def test_iteration_continue_uses_interaction_port(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    config.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\nagent:\n  max_iterations: 1\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.txt").write_text("sample", encoding="utf-8")
    monkeypatch.setattr(runtime_module, "LLMClient", _ToolClient)
    port = _StopContinuePort()
    session_runtime = AgentService(config_path=config, workspace_root=tmp_path).create_session(
        interaction=port
    )
    try:
        execution = session_runtime.start_run("read")
        assert list(execution.events)[-1].terminal_status == "failed"
        assert port.request is not None
        assert port.request.run_id == execution.run_id
        assert port.request.session_id == session_runtime.session.id
    finally:
        session_runtime.close()


class _BudgetThenDoneClient:
    def __init__(self, _provider) -> None:
        self.calls = 0

    def complete_stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall("call-1", "read_file", {"path": "sample.txt"}),
                    ToolCall("call-2", "read_file", {"path": "sample.txt"}),
                ],
            )
        else:
            yield StreamEvent(kind="content", text="done")


class _ContinueBudgetPort(SafeDefaultInteractionPort):
    def __init__(self) -> None:
        self.request = None

    def confirm_continue(self, request):
        self.request = request
        return ContinueDecision(request.request_id, continue_run=True)


def test_service_tool_budget_continuation_uses_same_interaction(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    config.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\nagent:\n  max_tool_calls: 1\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.txt").write_text("sample", encoding="utf-8")
    monkeypatch.setattr(runtime_module, "LLMClient", _BudgetThenDoneClient)
    port = _ContinueBudgetPort()
    session_runtime = AgentService(config_path=config, workspace_root=tmp_path).create_session(
        interaction=port
    )
    try:
        events = list(session_runtime.start_run("read twice").events)
        assert events[-1].terminal_status == "completed"
        assert port.request is not None
        assert port.request.reason == "tool_call_budget_exhausted"
        assert port.request.resource == "tool_calls"
        assert (port.request.used, port.request.limit) == (1, 1)
    finally:
        session_runtime.close()


class _ProviderFailureClient:
    def __init__(self, _provider) -> None:
        pass

    def complete_stream(self, messages, tools=None):
        yield StreamEvent(kind="error", text="secret raw exception")


def test_service_failed_terminal_is_unique_and_structured(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_module, "LLMClient", _ProviderFailureClient)
    session_runtime = AgentService(config_path=config, workspace_root=tmp_path).create_session()
    try:
        events = list(session_runtime.start_run("fail").events)
        terminals = [event for event in events if event.kind == "run_terminal"]
        assert len(terminals) == 1
        assert terminals[0].terminal_status == "failed"
        assert terminals[0].failure is not None
        assert terminals[0].failure.code == "internal_error"
        assert "secret raw exception" not in terminals[0].failure.safe_message
        assert not any(event.kind == "final" for event in events)
    finally:
        session_runtime.close()


def test_new_run_is_rejected_while_session_has_paused_run(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        execution = session_runtime.start_run("task")
        iterator = execution.events
        next(iterator)
        session_runtime.pause()
        list(iterator)
        with pytest.raises(SessionRunConflictError):
            session_runtime.start_run("fork")
    finally:
        session_runtime.close()


def test_cancel_through_public_session_runtime(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        execution = session_runtime.start_run("task")
        iterator = execution.events
        next(iterator)
        session_runtime.cancel()
        events = list(iterator)
        assert events[-1].terminal_status == "cancelled"
        saved = session_runtime.runtime.run_store.load(execution.run_id).document
        assert saved["status"] == "cancelled"
        assert saved["session_synced"] is True
    finally:
        session_runtime.close()


def test_service_lists_and_deletes_sessions(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    session_id = session_runtime.session.id
    session_runtime.close()
    assert [item.id for item in service.list_sessions()] == [session_id]
    assert service.list_runs(session_id=session_id) == []
    assert service.delete_session(session_id) is True
    assert service.list_sessions() == []


def test_service_inspects_and_deletes_terminal_run(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        execution = session_runtime.start_run("task")
        assert list(execution.events)[-1].terminal_status == "completed"
        info = service._inspect_run(execution.run_id)
        assert info.run_id == execution.run_id
        assert info.session_id == session_runtime.session.id
        assert info.provider == "fake"
    finally:
        session_runtime.close()
    assert service._delete_run(execution.run_id) is True
    assert service.list_runs() == []


class _RecordingPort(SafeDefaultInteractionPort):
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_create_session_failure_closes_runtime(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    port = _RecordingPort()

    def fail_save(self, session, messages):
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        service.create_session(interaction=port)
    assert port.closed is True


def test_runtime_provider_override_revalidates_context_window(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.yaml"
    path.write_text(
        """active: large
providers:
  large:
    model: openai/large
    context_window: 32000
  small:
    model: openai/small
    context_window: 4096
agent:
  max_context_tokens: 16000
""",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match="context_window"):
        runtime_module.create_runtime(
            config_path=path,
            workspace_root=tmp_path,
            interactive=False,
            provider="small",
        )


def test_session_runtime_construction_failure_closes_runtime(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    created = service.create_session()
    session_id = created.session.id
    created.close()
    port = _RecordingPort()

    monkeypatch.setattr(
        runtime_module.AgentLoop,
        "load_history",
        lambda self, messages: (_ for _ in ()).throw(ValueError("invalid history")),
    )
    with pytest.raises(ValueError, match="invalid history"):
        service.load_session(session_id, interaction=port)
    assert port.closed is True
