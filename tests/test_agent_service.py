"""公共 Session/Run 服务门面的端到端契约。"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from assistant_agent.bootstrap import runtime as runtime_module
from assistant_agent.contracts.events import StepEvent
from assistant_agent.contracts.failures import RunFailure
from assistant_agent.interaction import (
    BlockingInteractionPort,
    ContinueDecision,
    DefinitionChangeDecision,
    RecoveryDecision,
    SafeDefaultInteractionPort,
)
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.providers.ports import ProviderFailure, StreamEvent, ToolCall
from assistant_agent.service import (
    AgentService,
    ArtifactNotFoundError,
    ArtifactUnavailableError,
    IdempotencyConflictError,
    RunNotResumableError,
    RunNotRetryableError,
    RunStillActiveError,
    RuntimeConfigError,
    SessionBusyError,
    SessionNotFoundError,
    SessionRunConflictError,
)
from tests.support import ToolBudget


class _FakeClient:
    def __init__(self, _provider) -> None:
        pass

    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        yield StreamEvent(kind="content", text="done")


class _RetryBaselineClient:
    def __init__(self, _provider) -> None:
        self.messages: list[list[dict]] = []

    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        self.messages.append([dict(message) for message in messages])
        if len(self.messages) == 2:
            raise RuntimeError("provider failed")
        text = "seed-answer" if len(self.messages) == 1 else "retry-answer"
        yield StreamEvent(kind="content", text=text)


class _NativeOutputClient:
    instances: list[_NativeOutputClient] = []

    def __init__(self, _provider) -> None:
        self.calls = 0
        self.tools: list[list[dict]] = []
        self.__class__.instances.append(self)

    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        self.tools.append(list(tools or []))
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="create-html-1",
                        name="create_output",
                        arguments={
                            "filename": "admin.html",
                            "media_type": "text/html",
                            "title": "Admin",
                        },
                    )
                ],
            )
        elif self.calls == 2:
            yield StreamEvent(kind="content", text="<html><body>")
            yield StreamEvent(kind="content", text="后台</body></html>")
        else:
            yield StreamEvent(kind="content", text="后台页面文件已生成。")


class _OutputThenProviderFailureClient(_NativeOutputClient):
    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        self.tools.append(list(tools or []))
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="create-failed-1",
                        name="create_output",
                        arguments={"filename": "partial.html", "media_type": "text/html"},
                    )
                ],
            )
            return
        yield StreamEvent(kind="content", text="<html>partial")
        yield StreamEvent(
            kind="error",
            failure=ProviderFailure(
                code="provider_unavailable",
                safe_message="模型服务暂不可用。",
                retryable=True,
            ),
        )


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
        snapshot = session_runtime.run_snapshot(execution.run_id)
        assert snapshot.status == "completed"
        assert snapshot.terminal_status == "completed"
        assert snapshot.final_candidate == "done"
        assert snapshot.execution_status == "inactive"
        assert snapshot.budget.iterations_limit > 0
    finally:
        session_runtime.close()


def test_native_artifact_writer_captures_stream_without_assistant_delta(tmp_path, monkeypatch):
    config_path = _config(tmp_path, monkeypatch)
    config_path.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 32000\n  reserved_output_tokens: 4096\n",
        encoding="utf-8",
    )
    _NativeOutputClient.instances.clear()
    monkeypatch.setattr(runtime_module, "LLMClient", _NativeOutputClient)
    service = AgentService(config_path=config_path, workspace_root=tmp_path)
    session_runtime = service.create_session(interaction=SafeDefaultInteractionPort())
    try:
        execution = session_runtime.start_run("创建后台页面")
        events = list(execution.events)
        created = [event for event in events if event.output is not None]
        assert len(created) == 1
        assert created[0].kind == "tool_result"
        assert created[0].tool_name == "create_output"
        assert created[0].call_id == "create-html-1"
        assert created[0].result_code == "output_created"
        artifact = created[0].output
        assert artifact is not None
        assert artifact.call_id == "create-html-1"
        assert not any(event.kind == "content_delta" and "<html>" in event.text for event in events)
        client = _NativeOutputClient.instances[-1]
        assert client.calls == 2
        assert len(client.tools) == 2
        assert client.tools[1] == []
        final = next(event for event in events if event.kind == "final")
        assert final.text == "已生成文件：admin.html"
        assert events[-1].terminal_status == "completed"
        kinds = [event.kind for event in events]
        assert kinds.index("tool_result") < kinds.index("final") < kinds.index("run_terminal")
        assert kinds.count("run_terminal") == 1
        payload = session_runtime.get_output_payload(artifact.output_id)
        assert payload.content == "<html><body>后台</body></html>"
        snapshot = session_runtime.run_snapshot(execution.run_id)
        assert snapshot.outputs == (artifact,)
        assert snapshot.final_candidate == "已生成文件：admin.html"
        assert "<!DOCTYPE" not in (snapshot.final_candidate or "")
        session = session_runtime.snapshot()
        assert session.outputs == (artifact,)
        assistant_messages = [
            message for message in session.messages if message.role == "assistant"
        ]
        assert assistant_messages[-1].content == "已生成文件：admin.html"
        assert all("<!DOCTYPE" not in str(message.content) for message in session.messages)
        saved = session_runtime.runtime.session_store.load(session_runtime.session.id)
        assert all("<!DOCTYPE" not in str(message.content) for message in saved.message_ledger)
    finally:
        session_runtime.close()


def test_native_artifact_writer_provider_failure_publishes_no_partial_file(tmp_path, monkeypatch):
    config_path = _config(tmp_path, monkeypatch)
    config_path.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 32000\n  reserved_output_tokens: 4096\n",
        encoding="utf-8",
    )
    _OutputThenProviderFailureClient.instances.clear()
    monkeypatch.setattr(runtime_module, "LLMClient", _OutputThenProviderFailureClient)
    service = AgentService(config_path=config_path, workspace_root=tmp_path)
    session_runtime = service.create_session(interaction=SafeDefaultInteractionPort())
    try:
        execution = session_runtime.start_run("创建会失败的页面")
        events = list(execution.events)
        assert events[-1].terminal_status == "failed"
        assert not any(event.output is not None for event in events)
        assert session_runtime.run_snapshot(execution.run_id).outputs == ()
        assert session_runtime.runtime.output_store.list(session_runtime.session.id) == []
        draft_root = session_runtime.runtime.output_store.root / ".drafts"
        assert not list(draft_root.glob("**/chunk-*.txt"))
    finally:
        session_runtime.close()


def test_terminal_session_sync_preserves_concurrent_user_rename(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session(interaction=SafeDefaultInteractionPort())
    try:
        execution = session_runtime.start_run("task after stale snapshot")
        renamed = service.update_session_metadata(
            session_runtime.session.id,
            "user title",
            1,
        )
        assert list(execution.events)[-1].terminal_status == "completed"
        saved = session_runtime.runtime.session_store.load(session_runtime.session.id)
        assert saved.title == renamed.title == "user title"
        assert saved.title_source == "user"
        assert saved.metadata_version == 2
        assert saved.messages[-1] == {"role": "assistant", "content": "done"}
    finally:
        session_runtime.close()


def test_delete_holds_execution_lease_between_check_and_commit(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    stale_runtime = service.create_session()
    session_id = stale_runtime.session.id
    checked = threading.Event()
    release = threading.Event()
    original_list = service._run_store.list
    first_call = True

    def blocking_list():
        nonlocal first_call
        items = original_list()
        if first_call:
            first_call = False
            checked.set()
            assert release.wait(timeout=5)
        return items

    monkeypatch.setattr(service._run_store, "list", blocking_list)
    outcome = []
    delete_errors = []
    thread = threading.Thread(
        target=lambda: _call_with_errors(
            lambda: service.delete_session(session_id), outcome, delete_errors
        )
    )
    thread.start()
    try:
        assert checked.wait(timeout=5)
        with pytest.raises(RunStillActiveError):
            stale_runtime.start_run("must not start during delete")
    finally:
        release.set()
        thread.join(timeout=5)
        stale_runtime.close()
    assert not thread.is_alive()
    assert delete_errors == []
    assert outcome == [True]
    with pytest.raises(SessionNotFoundError):
        service.load_session(session_id)
    assert service.list_runs(session_id=session_id) == []


def test_force_delete_active_run_tombstones_session_and_run_writes(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    session_id = session_runtime.session.id
    execution = session_runtime.start_run("active delete")
    run_id = execution.run_id
    run_dir = session_runtime.runtime.run_store._dir
    first_event = next(execution.events)
    assert first_event.terminal_status is None
    assert (run_dir / f"{run_id}.json").is_file()

    assert service.delete_session(session_id, force=True) is True
    with pytest.raises(FileNotFoundError):
        list(execution.events)
    session_runtime.close()

    assert not session_runtime.runtime.session_store._path(session_id).exists()
    assert not list(run_dir.glob(f"{run_id}*.json"))
    assert service.list_runs(session_id=session_id) == []
    with pytest.raises(FileNotFoundError):
        session_runtime.runtime.session_store.save(
            session_runtime.session,
            [{"role": "user", "content": "must not revive"}],
        )
    assert not list(run_dir.glob(f"{run_id}*.json"))


def test_force_delete_waits_for_terminal_session_write_then_removes_all(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    session_id = session_runtime.session.id
    execution = session_runtime.start_run("terminal delete race")
    run_id = execution.run_id
    run_dir = session_runtime.runtime.run_store._dir
    writing = threading.Event()
    release_write = threading.Event()
    original_write = SessionStore._atomic_write_locked

    def blocking_write(store, session):
        if session.messages and session.messages[-1].get("content") == "done":
            writing.set()
            assert release_write.wait(timeout=5)
        return original_write(store, session)

    monkeypatch.setattr(SessionStore, "_atomic_write_locked", blocking_write)
    execution_errors = []
    execution_thread = threading.Thread(
        target=lambda: _consume_with_errors(execution.events, execution_errors)
    )
    delete_result = []
    delete_thread = threading.Thread(
        target=lambda: delete_result.append(service.delete_session(session_id, force=True))
    )
    execution_thread.start()
    try:
        assert writing.wait(timeout=5)
        delete_thread.start()
        assert delete_thread.is_alive()
    finally:
        release_write.set()
        execution_thread.join(timeout=5)
        delete_thread.join(timeout=5)
        session_runtime.close()
    assert not execution_thread.is_alive()
    assert not delete_thread.is_alive()
    assert all(isinstance(error, FileNotFoundError) for error in execution_errors)
    assert delete_result == [True]
    assert not session_runtime.runtime.session_store._path(session_id).exists()
    assert not list(run_dir.glob(f"{run_id}*.json"))
    assert service.list_runs(session_id=session_id) == []


def _consume_with_errors(events, errors):
    try:
        list(events)
    except Exception as exc:  # noqa: BLE001 - test captures the race outcome for cleanup
        errors.append(exc)


def _call_with_errors(call, results, errors):
    try:
        results.append(call())
    except Exception as exc:  # noqa: BLE001 - test reports worker failures explicitly
        errors.append(exc)


def test_active_lease_blocks_resume_and_reconcile(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    execution = first.start_run("task")
    iterator = execution.events
    next(iterator)
    second = service.load_session(first.session.id)
    try:
        with pytest.raises(RunStillActiveError):
            second.resume_run(execution.run_id)
        with pytest.raises(RunStillActiveError):
            second.reconcile_orphaned_run(execution.run_id, "reconcile-1")
    finally:
        iterator.close()
        second.close()
        first.close()


def test_execution_close_releases_unstarted_lease(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    execution = first.start_run("never-started")
    execution.close()
    assert first.active_run_id is None
    assert first.run_snapshot(execution.run_id).status == "cancelled"

    second = service.load_session(first.session.id)
    try:
        follow_up = second.start_run("next")
        follow_up.close()
    finally:
        second.close()
        first.close()


def test_execution_close_releases_partially_iterated_lease(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    execution = first.start_run("partial")
    next(execution.events)
    execution.close()
    assert first.run_snapshot(execution.run_id).status == "paused"

    second = service.load_session(first.session.id)
    try:
        resumed = second.resume_run(execution.run_id)
        resumed.close()
    finally:
        second.close()
        first.close()


def test_runtime_close_releases_unstarted_execution_lease(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    execution = first.start_run("runtime-close")
    first.runtime.close("test")
    assert first.run_snapshot(execution.run_id).status == "cancelled"

    second = service.load_session(first.session.id)
    try:
        follow_up = second.start_run("next")
        follow_up.close()
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize("close_owner", ["execution", "runtime"])
def test_concurrent_close_keeps_lease_until_blocked_worker_exits(
    tmp_path, monkeypatch, close_owner
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def __init__(self, _provider) -> None:
            pass

        def complete_stream(self, messages, tools=None):
            entered.set()
            assert release.wait(timeout=5)
            yield StreamEvent(kind="content", text="late")

    config = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_module, "LLMClient", BlockingClient)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    first = service.create_session()
    execution = first.start_run("blocked-provider")
    worker_errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(execution.events)
        except BaseException as exc:  # pragma: no cover - assertion reports details
            worker_errors.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    second = None
    try:
        worker.start()
        assert entered.wait(timeout=2)
        if close_owner == "execution":
            execution.close()
        else:
            first.runtime.close("concurrent-close-test")

        assert worker.is_alive()
        second = service.load_session(first.session.id)
        with pytest.raises(RunStillActiveError):
            second.runtime.execution_leases.acquire(first.session.id)

        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert worker_errors == []

        lease = second.runtime.execution_leases.acquire(first.session.id)
        lease.release()
    finally:
        release.set()
        worker.join(timeout=2)
        if second is not None:
            second.close()
        first.close()


def test_reconcile_is_idempotent_and_pauses_orphan(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    coordinator = first.runtime.new_run("task", first.session.id)
    assert coordinator is not None
    coordinator.initialize([{"role": "user", "content": "task"}], None, ToolBudget(max_calls=10))
    run_id = coordinator.run_id
    session_id = first.session.id
    first.close()

    recovered = service.load_session(session_id)
    try:
        with pytest.raises(RunNotResumableError):
            recovered.resume_run(run_id)
        result = recovered.reconcile_orphaned_run(run_id, "reconcile-1")
        assert list(result.events)[-1].terminal_status == "paused"
        duplicate = recovered.reconcile_orphaned_run(run_id, "reconcile-1")
        assert list(duplicate.events) == []
        snapshot = recovered.run_snapshot(run_id)
        assert snapshot.status == "paused"
        assert snapshot.execution_status == "inactive"
        assert snapshot.allowed_actions == ("resume_run", "stop")
    finally:
        recovered.close()


def test_retry_failed_run_creates_one_linked_run_for_same_key(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        original = session_runtime.runtime.new_run("task", session_runtime.session.id)
        assert original is not None
        original.initialize([{"role": "user", "content": "task"}], None, ToolBudget(max_calls=10))
        original.terminal(
            success=False,
            text="provider timeout",
            messages=original.state.messages,
            compaction_checkpoint=None,
            failure=RunFailure(
                code="provider_timeout",
                safe_message="模型请求超时。",
                retryable=True,
                allowed_actions=("retry_run", "start_new_run"),
                terminal_status="failed",
                phase="calling_model",
            ),
        )
        retried = session_runtime.retry_failed_run(original.run_id, "retry-1")
        assert retried.created is True
        assert list(retried.events)[-1].terminal_status == "completed"
        duplicate = session_runtime.retry_failed_run(original.run_id, "retry-1")
        assert duplicate.created is False
        assert duplicate.new_run_id == retried.new_run_id
        assert list(duplicate.events) == []
        snapshot = session_runtime.run_snapshot(retried.new_run_id)
        assert snapshot.retry_of_run_id == original.run_id
    finally:
        session_runtime.close()


def test_retry_rebuilds_original_session_baseline_without_duplicate_user_message(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_module, "LLMClient", _RetryBaselineClient)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        seed = session_runtime.start_run("seed")
        list(seed.events)
        failed = session_runtime.start_run("analyze")
        assert list(failed.events)[-1].terminal_status == "failed"
        original = session_runtime.runtime.run_store.load(failed.run_id).document
        assert original["baseline_messages"] == [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "seed-answer"},
        ]

        retried = session_runtime.retry_failed_run(failed.run_id, "retry-baseline")
        assert list(retried.events)[-1].terminal_status == "completed"
        client = session_runtime.runtime.loop._client  # noqa: SLF001
        assert isinstance(client, _RetryBaselineClient)
        assert client.messages[2] == client.messages[1]
        assert [
            message
            for message in client.messages[2]
            if message == {"role": "user", "content": "analyze"}
        ] == [{"role": "user", "content": "analyze"}]
    finally:
        session_runtime.close()


def test_retry_rebuilds_when_recorded_target_was_pruned(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        original = session_runtime.runtime.new_run("task", session_runtime.session.id)
        assert original is not None
        original.initialize([], None, ToolBudget(max_calls=10))
        original.terminal(
            success=False,
            text="timeout",
            messages=[],
            compaction_checkpoint=None,
            failure=RunFailure(
                code="provider_timeout",
                safe_message="模型请求超时。",
                retryable=True,
                allowed_actions=("retry_run",),
                terminal_status="failed",
                phase="calling_model",
            ),
        )
        first = session_runtime.retry_failed_run(original.run_id, "retry-pruned")
        list(first.events)
        assert session_runtime.runtime.run_store.delete(first.new_run_id) is True

        rebuilt = session_runtime.retry_failed_run(original.run_id, "retry-pruned")
        assert rebuilt.created is True
        assert rebuilt.new_run_id != first.new_run_id
        list(rebuilt.events)
        saved = session_runtime.runtime.run_store.load(original.run_id).document
        assert set(saved["retry_requests"].values()) == {rebuilt.new_run_id}
    finally:
        session_runtime.close()


@pytest.mark.parametrize(
    ("failure_point", "notice_code"),
    [
        ("session", "session_sync_deferred"),
        ("prune", "run_prune_deferred"),
    ],
)
def test_terminal_is_published_once_when_finalization_fails(
    tmp_path, monkeypatch, failure_point, notice_code
):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    if failure_point == "session":
        monkeypatch.setattr(
            session_runtime.runtime.session_store,
            "save",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
    else:
        monkeypatch.setattr(
            session_runtime.runtime.run_store,
            "prune",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("prune failed")),
        )
    try:
        execution = session_runtime.start_run("task")
        events = list(execution.events)
        terminals = [event for event in events if event.kind == "run_terminal"]
        assert len(terminals) == 1
        assert terminals[0].terminal_status == "completed"
        assert any(event.kind == "notice" and event.result_code == notice_code for event in events)
        persisted = session_runtime.runtime.run_store.load(execution.run_id).document
        assert persisted["status"] == "completed"
        if failure_point == "session":
            assert persisted["session_synced"] is False
    finally:
        session_runtime.close()


def test_unsafe_failed_run_cannot_be_retried(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        original = session_runtime.runtime.new_run("task", session_runtime.session.id)
        assert original is not None
        original.initialize([], None, ToolBudget(max_calls=10))
        original.state.retry_safety = "unsafe"
        original.terminal(
            success=False,
            text="failed",
            messages=[],
            compaction_checkpoint=None,
            failure=RunFailure(
                code="internal_error",
                safe_message="任务执行失败。",
                retryable=True,
                allowed_actions=("retry_run", "start_new_run"),
                terminal_status="failed",
                phase="saving_checkpoint",
            ),
        )
        with pytest.raises(RunNotRetryableError):
            session_runtime.retry_failed_run(original.run_id, "retry-unsafe")
    finally:
        session_runtime.close()


def test_retry_rejects_session_busy_and_key_reuse_for_other_run(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()

    def failed_run():
        coordinator = session_runtime.runtime.new_run("task", session_runtime.session.id)
        assert coordinator is not None
        coordinator.initialize([], None, ToolBudget(max_calls=10))
        coordinator.terminal(
            success=False,
            text="timeout",
            messages=[],
            compaction_checkpoint=None,
            failure=RunFailure(
                code="provider_timeout",
                safe_message="模型请求超时。",
                retryable=True,
                allowed_actions=("retry_run",),
                terminal_status="failed",
                phase="calling_model",
            ),
        )
        return coordinator

    try:
        first = failed_run()
        first_retry = session_runtime.retry_failed_run(first.run_id, "shared-key")
        list(first_retry.events)
        second = failed_run()
        with pytest.raises(IdempotencyConflictError):
            session_runtime.retry_failed_run(second.run_id, "shared-key")

        orphan = session_runtime.runtime.new_run("unfinished", session_runtime.session.id)
        assert orphan is not None
        orphan.initialize([], None, ToolBudget(max_calls=10))
        third = failed_run()
        with pytest.raises(SessionBusyError):
            session_runtime.retry_failed_run(third.run_id, "third-key")
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
                            "schema_version": 2,
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


class _ChartThenFailClient(_ChartClient):
    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        if self.calls == 0:
            yield from super().complete_stream(messages, tools)
            return
        raise RuntimeError("provider failed after chart")


class _InvalidChartTwiceClient:
    def __init__(self, _provider) -> None:
        self.calls = 0

    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        self.calls += 1
        if self.calls <= 2:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        f"invalid-chart-{self.calls}",
                        "present_chart",
                        {
                            "chart_type": "bar",
                            "title": "无效图表",
                            "columns": [{"key": "value", "label": "数量"}],
                            "rows": [[None]],
                            "x_key": "value",
                            "series": [{"key": "value", "label": "数量"}],
                        },
                    )
                ],
            )
            return
        yield StreamEvent(kind="content", text="图表未创建，文字结论仍然完整。")


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


def test_repeated_invalid_chart_keeps_single_completed_terminal(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    config.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 16000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_module, "LLMClient", _InvalidChartTwiceClient)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        events = list(session_runtime.start_run("画图").events)
        failures = [
            item
            for item in events
            if item.kind == "tool_result" and item.result_code == "artifact_rejected"
        ]
        assert len(failures) == 2
        assert failures[0].failure.retryable is True
        assert failures[1].failure.retryable is False
        assert any(item.kind == "final" for item in events)
        terminals = [item for item in events if item.kind == "run_terminal"]
        assert len(terminals) == 1
        assert terminals[0].terminal_status == "completed"
    finally:
        session_runtime.close()


def test_failed_run_keeps_chart_bound_to_authoritative_assistant_message(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    config.write_text(
        "active: fake\nproviders:\n  fake:\n    model: openai/fake\n"
        "agent:\n  max_context_tokens: 16000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_module, "LLMClient", _ChartThenFailClient)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    session_runtime = service.create_session()
    try:
        events = list(session_runtime.start_run("画图后失败").events)
        assert events[-1].terminal_status == "failed"
        chart = next(item.chart for item in events if item.chart is not None)
        snapshot = session_runtime.snapshot()
        message = next(item for item in snapshot.messages if item.id == chart.message_id)
        assert message.role == "assistant"
        assert message.content == ""
        assert message.artifacts == (chart.ref,)
        assert message.reply_to_message_id == snapshot.messages[0].id
    finally:
        session_runtime.close()


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
        reconciled = resumed_runtime.reconcile_orphaned_run(run_id, "reconcile-test")
        assert list(reconciled.events)[-1].terminal_status == "paused"
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


def _pause_run(session_runtime):
    execution = session_runtime.start_run("task")
    iterator = execution.events
    next(iterator)
    session_runtime.pause()
    assert list(iterator)[-1].terminal_status == "paused"
    return execution


def test_cancel_paused_run_persists_terminal_syncs_and_allows_session_delete(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    session_id = session_runtime.session.id
    execution = _pause_run(session_runtime)
    session_runtime.close()

    recovered_runtime = service.load_session(session_id)
    cancelled = recovered_runtime.cancel_run(execution.run_id)
    events = list(cancelled.events)
    assert len(events) == 1
    assert events[0].kind == "run_terminal"
    assert events[0].terminal_status == "cancelled"
    saved = recovered_runtime.runtime.run_store.load(execution.run_id).document
    assert saved["status"] == "cancelled"
    assert saved["phase"] == "terminal"
    assert saved["session_synced"] is True

    assert list(recovered_runtime.cancel_run(execution.run_id).events) == []
    recovered_runtime.close()
    assert service.delete_session(session_id) is True


def test_cancel_run_rejects_wrong_session_active_and_completed_runs(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    first = service.create_session()
    second = service.create_session()
    try:
        active = first.start_run("active")
        with pytest.raises(SessionBusyError):
            first.cancel_run(active.run_id)
        first.cancel()
        assert list(active.events)[-1].terminal_status == "cancelled"

        completed = second.start_run("complete")
        assert list(completed.events)[-1].terminal_status == "completed"
        with pytest.raises(SessionRunConflictError, match="completed"):
            second.cancel_run(completed.run_id)
        with pytest.raises(SessionRunConflictError, match="不属于"):
            first.cancel_run(completed.run_id)
    finally:
        first.close()
        second.close()


def test_event_source_exception_is_persisted_as_unique_failed_terminal(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    session_id = session_runtime.session.id

    def broken_source(*_args, **_kwargs):
        yield StepEvent(kind="content_delta", text="partial")
        raise RuntimeError("api_key=secret-value")

    monkeypatch.setattr(session_runtime.runtime.loop, "run", broken_source)
    execution = session_runtime.start_run("task")
    events = list(execution.events)
    terminals = [event for event in events if event.kind == "run_terminal"]
    assert len(terminals) == 1
    assert terminals[0].terminal_status == "failed"
    assert terminals[0].failure is not None
    assert terminals[0].failure.code == "internal_error"
    assert "secret-value" not in terminals[0].text
    assert "secret-value" not in terminals[0].failure.safe_message
    saved = session_runtime.runtime.run_store.load(execution.run_id).document
    assert saved["status"] == "failed"
    assert saved["phase"] == "terminal"
    assert saved["session_synced"] is True
    assert "secret-value" not in str(saved)

    session_runtime.close()
    assert service.delete_session(session_id) is True


def test_closing_event_consumer_still_safely_pauses_run(tmp_path, monkeypatch):
    session_runtime = AgentService(
        config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path
    ).create_session()
    try:
        execution = session_runtime.start_run("task")
        iterator = execution.events
        next(iterator)
        iterator.close()
        saved = session_runtime.runtime.run_store.load(execution.run_id).document
        assert saved["status"] == "paused"
        assert saved["phase"] != "terminal"
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


def test_force_delete_active_run_blocks_delayed_checkpoint_and_restart(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    session_runtime = service.create_session()
    execution = session_runtime.start_run("delete active run")
    run_id = execution.run_id
    store = session_runtime.runtime.run_store
    next(execution.events)
    stale = store.load(run_id).document
    with pytest.raises(SessionRunConflictError, match="Run 尚未结束"):
        service._delete_run(run_id)
    attempting_save = threading.Event()
    release_save = threading.Event()
    original_save = store._save_with_run_lock

    def delayed_save(target_run_id, payload):
        if target_run_id == run_id:
            attempting_save.set()
            assert release_save.wait(timeout=5)
        return original_save(target_run_id, payload)

    monkeypatch.setattr(store, "_save_with_run_lock", delayed_save)
    execution_errors = []
    worker = threading.Thread(
        target=lambda: _consume_with_errors(execution.events, execution_errors)
    )
    worker.start()
    try:
        assert attempting_save.wait(timeout=5)
        assert service._delete_run(run_id, force=True) is True
    finally:
        release_save.set()
        worker.join(timeout=5)
        session_runtime.close()

    assert not worker.is_alive()
    assert any(isinstance(error, FileNotFoundError) for error in execution_errors)
    assert service._delete_run(run_id, force=True) is False
    assert not list(store._dir.glob(f"{run_id}*.json"))
    restarted = RunStore(store._dir)
    with pytest.raises(FileNotFoundError):
        restarted.load(run_id)
    with pytest.raises(FileNotFoundError):
        restarted.save(run_id, stale)
    assert not list(store._dir.glob(f"{run_id}*.json"))


class _RecordingPort(SafeDefaultInteractionPort):
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_create_session_failure_closes_runtime(tmp_path, monkeypatch):
    service = AgentService(config_path=_config(tmp_path, monkeypatch), workspace_root=tmp_path)
    port = _RecordingPort()

    def fail_save(self, session, messages, **_kwargs):
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
