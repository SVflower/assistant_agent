from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from assistant_agent.application.sessions import AgentService
from assistant_agent.config.schema import OutputConfig
from assistant_agent.contracts.attachments import MessageContentV1, TextPartV1
from assistant_agent.contracts.outputs import (
    OutputConflictError,
    OutputInvalidError,
    OutputLimitExceededError,
    OutputNotFoundError,
    OutputUnavailableError,
)
from assistant_agent.contracts.sessions import PublicMessageSnapshot
from assistant_agent.persistence.outputs import OutputStore
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.tools.outputs import (
    CreateOutputTool,
    ManageOutputTool,
)


def _store(tmp_path: Path, **updates: object) -> OutputStore:
    return OutputStore(tmp_path, OutputConfig(**updates))


def _publish(store: OutputStore, **updates: object):
    values = {
        "session_id": "session-1",
        "run_id": "run-1",
        "call_id": "call-1",
        "filename": "report.html",
        "media_type": "text/html",
        "content": "<h1>ok</h1>",
        "message_id": "msg_" + "a" * 24,
    }
    values.update(updates)
    return store.publish_text(**values)


def test_store_uses_date_session_layout_and_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _publish(store)
    path = store.local_path("session-1", artifact.output_id)
    assert path.relative_to(tmp_path / "outputs").parts[-2:] == (
        "session-1",
        f"{artifact.output_id}--report.html",
    )
    assert store.get_payload("session-1", artifact.output_id).content == "<h1>ok</h1>"
    assert artifact.preview_supported is True


def test_store_rejects_escape_unknown_mime_and_limits(tmp_path: Path) -> None:
    store = _store(tmp_path, max_file_bytes=1024, max_run_bytes=1024, max_session_bytes=1024)
    with pytest.raises(OutputInvalidError):
        _publish(store, filename="../escape.html")
    with pytest.raises(OutputInvalidError):
        _publish(store, media_type="application/pdf")
    with pytest.raises(OutputLimitExceededError):
        _publish(store, content="x" * 1025)


def test_store_is_idempotent_and_detects_conflict_or_corruption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _publish(store)
    assert _publish(store) == first
    with pytest.raises(OutputConflictError):
        _publish(store, content="different")
    store.local_path("session-1", first.output_id).write_text("tampered", encoding="utf-8")
    with pytest.raises(OutputUnavailableError):
        store.get_payload("session-1", first.output_id)


def test_delete_session_isolated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _publish(store)
    other = _publish(store, session_id="session-2", run_id="run-2", call_id="call-2")
    store.delete_session("session-1")
    assert store.list("session-1") == []
    assert store.get("session-2", other.output_id) == other
    assert first.output_id != other.output_id


@dataclass
class _Context:
    output_store: OutputStore
    current_session_id: str = "session-1"
    current_run_id: str = "run-1"
    current_call_id: str = "call-1"


def test_create_output_tool_is_safe_idempotent(tmp_path: Path) -> None:
    tool = CreateOutputTool()
    ctx = _Context(_store(tmp_path))
    args = {"filename": "data.csv", "media_type": "text/csv", "content": "x,y\n1,2"}
    assert tool.replay_policy(args, ctx, []) == "safe_idempotent"  # type: ignore[arg-type]
    result = tool.run(args, ctx)  # type: ignore[arg-type]
    assert result.code == "output_created"
    assert result.output_artifact is not None
    assert result.output_artifact.message_id is not None


def test_chunked_output_round_trip_is_ordered_idempotent_and_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path, max_chunk_bytes=4096)
    draft_id = store.begin_text_draft(
        session_id="session-1",
        run_id="run-1",
        call_id="begin-1",
        filename="admin.html",
        media_type="text/html",
        message_id="msg_" + "a" * 24,
    )
    assert store.list("session-1") == []
    first = "<html>你好"
    assert store.append_text_draft(
        session_id="session-1",
        run_id="run-1",
        draft_id=draft_id,
        chunk_index=0,
        content=first,
    ) == len(first.encode())
    assert store.append_text_draft(
        session_id="session-1",
        run_id="run-1",
        draft_id=draft_id,
        chunk_index=0,
        content=first,
    ) == len(first.encode())
    store.append_text_draft(
        session_id="session-1",
        run_id="run-1",
        draft_id=draft_id,
        chunk_index=1,
        content="</html>",
    )
    artifact = store.finalize_text_draft(session_id="session-1", run_id="run-1", draft_id=draft_id)
    assert (
        store.finalize_text_draft(session_id="session-1", run_id="run-1", draft_id=draft_id)
        == artifact
    )
    assert store.get_payload("session-1", artifact.output_id).content == "<html>你好</html>"


def test_chunked_output_enforces_utf8_byte_limit_order_and_run_ownership(tmp_path: Path) -> None:
    store = _store(tmp_path, max_chunk_bytes=1024)
    draft_id = store.begin_text_draft(
        session_id="session-1",
        run_id="run-1",
        call_id="begin-1",
        filename="admin.html",
        media_type="text/html",
    )
    with pytest.raises(OutputLimitExceededError):
        store.append_text_draft(
            session_id="session-1",
            run_id="run-1",
            draft_id=draft_id,
            chunk_index=0,
            content="汉" * 342,
        )
    with pytest.raises(OutputInvalidError):
        store.append_text_draft(
            session_id="session-1",
            run_id="run-1",
            draft_id=draft_id,
            chunk_index=1,
            content="later",
        )
    with pytest.raises(OutputNotFoundError):
        store.append_text_draft(
            session_id="session-1",
            run_id="run-2",
            draft_id=draft_id,
            chunk_index=0,
            content="wrong run",
        )


def test_chunked_output_tools_publish_only_on_finalize(tmp_path: Path) -> None:
    ctx = _Context(_store(tmp_path))
    tool = ManageOutputTool()
    started = tool.run(
        {"action": "begin", "filename": "admin.html", "media_type": "text/html"},
        ctx,  # type: ignore[arg-type]
    )
    draft_id = str(started.metadata["draft_id"])
    ctx.current_call_id = "append-1"
    appended = tool.run(
        {
            "action": "append",
            "draft_id": draft_id,
            "chunk_index": 0,
            "content": "<h1>ok</h1>",
        },
        ctx,  # type: ignore[arg-type]
    )
    assert appended.code == "output_chunk_appended"
    assert ctx.output_store.list("session-1") == []
    ctx.current_call_id = "finalize-1"
    finalized = tool.run(
        {"action": "finalize", "draft_id": draft_id},
        ctx,  # type: ignore[arg-type]
    )
    assert finalized.code == "output_created"
    assert finalized.output_artifact is not None


def test_discard_run_drafts_is_isolated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.begin_text_draft(
        session_id="session-1",
        run_id="run-1",
        call_id="begin-1",
        filename="one.txt",
        media_type="text/plain",
    )
    second = store.begin_text_draft(
        session_id="session-1",
        run_id="run-2",
        call_id="begin-2",
        filename="two.txt",
        media_type="text/plain",
    )
    store.discard_run_drafts("session-1", "run-1")
    with pytest.raises(OutputNotFoundError):
        store.append_text_draft(
            session_id="session-1",
            run_id="run-1",
            draft_id=first,
            chunk_index=0,
            content="gone",
        )
    assert (
        store.append_text_draft(
            session_id="session-1",
            run_id="run-2",
            draft_id=second,
            chunk_index=0,
            content="kept",
        )
        == 4
    )


def test_session_fork_deep_copies_output_payload_and_identity(tmp_path: Path) -> None:
    output_store = _store(tmp_path)
    session_store = SessionStore(tmp_path / "sessions", output_store=output_store)
    source = session_store.new_session()
    user_id = "msg_" + "1" * 24
    assistant_id = "msg_" + "2" * 24
    next_user_id = "msg_" + "3" * 24
    output = output_store.publish_text(
        session_id=source.id,
        run_id="run-source",
        call_id="call-source",
        message_id=assistant_id,
        filename="report.html",
        media_type="text/html",
        content="<h1>source</h1>",
    )
    source.messages = [
        {
            "role": "user",
            "content": MessageContentV1(parts=(TextPartV1(text="make"),)).model_dump(mode="json"),
        },
        {"role": "assistant", "content": "ready"},
        {
            "role": "user",
            "content": MessageContentV1(parts=(TextPartV1(text="next"),)).model_dump(mode="json"),
        },
    ]
    source.message_ledger = [
        PublicMessageSnapshot(
            id=user_id,
            role="user",
            content=MessageContentV1(parts=(TextPartV1(text="make"),)),
        ),
        PublicMessageSnapshot(
            id=assistant_id,
            role="assistant",
            reply_to_message_id=user_id,
            content="ready",
            outputs=(output,),
        ),
        PublicMessageSnapshot(
            id=next_user_id,
            role="user",
            content=MessageContentV1(parts=(TextPartV1(text="next"),)),
        ),
    ]
    source.outputs = [output]
    session_store.save(source, must_exist=False)

    forked, created = session_store.fork_session(source.id, next_user_id, "a" * 64, "b" * 64)

    assert created is True
    assert len(forked.outputs) == 1
    copied = forked.outputs[0]
    assert copied.session_id == forked.id
    assert copied.output_id != output.output_id
    assert copied.content_hash == output.content_hash
    assert output_store.local_path(forked.id, copied.output_id) != output_store.local_path(
        source.id, output.output_id
    )
    assert output_store.get_payload(forked.id, copied.output_id).content == "<h1>source</h1>"


class _Lease:
    def release(self) -> None:
        pass


class _Leases:
    def acquire(self, _session_id: str) -> _Lease:
        return _Lease()


class _Attachments:
    def delete_session(self, _session_id: str) -> None:
        pass


def test_agent_service_delete_session_cascades_output_payload(tmp_path: Path) -> None:
    output_store = _store(tmp_path)
    session_store = SessionStore(tmp_path / "sessions", output_store=output_store)
    session = session_store.new_session()
    session_store.save(session, [], must_exist=False)
    output = output_store.publish_text(
        session_id=session.id,
        run_id="run-delete",
        call_id="call-delete",
        filename="data.csv",
        media_type="text/csv",
        content="x\n1",
    )
    service = AgentService(
        runtime_factory=None,  # type: ignore[arg-type]
        session_store=session_store,
        run_store=RunStore(tmp_path / "runs"),
        session_leases=_Leases(),  # type: ignore[arg-type]
        max_completed_runs=10,
        attachment_store=_Attachments(),  # type: ignore[arg-type]
        output_store=output_store,
    )

    assert service.delete_session(session.id) is True
    assert output_store.list(session.id) == []
    with pytest.raises(OutputNotFoundError):
        output_store.get_payload(session.id, output.output_id)
