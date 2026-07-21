"""M23-R2 权威消息 ledger 与 Session fork 冻结契约。"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

import assistant_agent.contracts as contracts
import assistant_agent.service as service_contract
from assistant_agent.agent.run.state import canonical_hash
from assistant_agent.bootstrap import runtime as runtime_module
from assistant_agent.contracts.charts import (
    ChartColumn,
    ChartSeries,
    ChartSpecV1,
    build_chart_artifact,
)
from assistant_agent.contracts.errors import (
    IdempotencyConflictError,
    InvalidForkRequestError,
    InvalidIdempotencyKeyError,
    SessionMigrationRequiredError,
    UserMessageNotFoundError,
)
from assistant_agent.contracts.events import EVENT_CONTRACT_VERSION
from assistant_agent.contracts.sessions import PublicMessageSnapshot
from assistant_agent.persistence.store import SessionStore
from assistant_agent.providers.ports import StreamEvent
from assistant_agent.service import AgentService


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


def _store(tmp_path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def _fork_hashes(source_id: str, boundary: str, key: str) -> tuple[str, str]:
    return (
        canonical_hash({"operation": "fork-key", "source_session_id": source_id, "key": key}),
        canonical_hash(
            {
                "operation": "fork-session",
                "source_session_id": source_id,
                "before_user_message_id": boundary,
            }
        ),
    )


def _source_with_three_turns(store: SessionStore):
    session = store.new_session(provider="fake", model="openai/fake")
    messages = []
    ledger = []
    for index in range(3):
        user_id = f"msg_{index * 2 + 1:024x}"
        assistant_id = f"msg_{index * 2 + 2:024x}"
        messages.extend(
            [
                {"role": "user", "content": f"user-{index}"},
                {"role": "assistant", "content": f"assistant-{index}"},
            ]
        )
        ledger.extend(
            [
                PublicMessageSnapshot(
                    id=user_id,
                    role="user",
                    created_at=None,
                    content=f"user-{index}",
                ),
                PublicMessageSnapshot(
                    id=assistant_id,
                    role="assistant",
                    created_at=None,
                    reply_to_message_id=user_id,
                    content=f"assistant-{index}",
                ),
            ]
        )
    session.messages = messages
    session.message_ledger = ledger
    session.compaction_checkpoint = {"summary": "boundary-after-secret"}
    store.save(session, must_exist=False)
    return session


def test_public_contract_exports_schema_v2_without_event_or_run_upgrade():
    assert contracts.SESSION_CONTRACT_VERSION == 2
    assert service_contract.SESSION_CONTRACT_VERSION == 2
    assert EVENT_CONTRACT_VERSION == 1
    assert contracts.PublicMessageSnapshot is service_contract.PublicMessageSnapshot
    assert contracts.SessionSnapshot is service_contract.SessionSnapshot
    for name in (
        "InvalidForkRequestError",
        "InvalidIdempotencyKeyError",
        "IdempotencyConflictError",
        "SessionMigrationRequiredError",
        "UserMessageNotFoundError",
    ):
        assert getattr(contracts, name) is getattr(service_contract, name)


def test_session_snapshot_rejects_assistant_to_assistant_reply():
    user = PublicMessageSnapshot(id="msg_111111111111111111111111", role="user")
    first = PublicMessageSnapshot(
        id="msg_222222222222222222222222",
        role="assistant",
        reply_to_message_id=user.id,
    )
    second = PublicMessageSnapshot(
        id="msg_333333333333333333333333",
        role="assistant",
        reply_to_message_id=first.id,
    )
    with pytest.raises(ValidationError, match="user message"):
        contracts.SessionSnapshot(id="session", messages=(user, first, second))


def test_v1_migration_assigns_stable_ids_null_times_and_explicit_replies(tmp_path):
    store = _store(tmp_path)
    path = store._path("legacy-v1")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "legacy-v1",
                "title": "legacy",
                "title_source": "auto",
                "metadata_version": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "messages": [
                    {"role": "user", "content": "question"},
                    {
                        "role": "assistant",
                        "content": "answer",
                        "created_at": "2026-01-01T00:01:00Z",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    first = store.load("legacy-v1")
    migrated_bytes = path.read_bytes()
    second = store.load("legacy-v1")

    assert first == second
    assert path.read_bytes() == migrated_bytes
    assert first.schema_version == 2
    assert [message.id for message in first.message_ledger] == [
        message.id for message in second.message_ledger
    ]
    user, assistant = first.message_ledger
    assert user.created_at is None
    assert user.reply_to_message_id is None
    assert assistant.created_at == "2026-01-01T00:01:00Z"
    assert assistant.reply_to_message_id == user.id


def test_v1_migration_projects_complete_chart_artifact_into_public_ledger(tmp_path):
    store = _store(tmp_path)
    path = store._path("legacy-v1-chart")
    path.parent.mkdir(parents=True)
    spec = ChartSpecV1(
        chart_type="bar",
        title="旧图表",
        columns=(
            ChartColumn(key="name", label="名称", data_type="string"),
            ChartColumn(key="value", label="数量", data_type="number"),
        ),
        rows=(("A", 1),),
        x_key="name",
        series=(ChartSeries(key="value", label="数量"),),
    )
    artifact = build_chart_artifact(
        spec,
        session_id="legacy-v1-chart",
        run_id="run-legacy-chart",
        call_id="call-legacy-chart",
        created_at="2026-01-01T00:01:00Z",
    )
    document = {
        "schema_version": 1,
        "id": "legacy-v1-chart",
        "title": "legacy chart",
        "title_source": "auto",
        "metadata_version": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "messages": [
            {"role": "user", "content": "chart"},
            {"role": "assistant", "content": "ready"},
        ],
        "assistant_messages": [
            {
                "id": artifact.message_id,
                "role": "assistant",
                "content": "ready",
                "artifacts": [artifact.ref.model_dump(mode="json")],
            }
        ],
        "presentations": [artifact.model_dump(mode="json")],
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    migrated = store.load("legacy-v1-chart")
    migrated_bytes = path.read_bytes()
    repeated = store.load("legacy-v1-chart")

    assert migrated == repeated
    assert path.read_bytes() == migrated_bytes
    assert migrated.schema_version == 2
    assert migrated.presentations == [artifact]
    assert migrated.assistant_messages[0].artifacts == (artifact.ref,)
    assert migrated.message_ledger[1].id == artifact.message_id
    assert migrated.message_ledger[1].artifacts == (artifact.ref,)


@pytest.mark.parametrize(
    ("boundary_index", "expected_count"),
    [(0, 0), (1, 2), (2, 4)],
)
def test_fork_uses_exclusive_user_boundaries_and_ignores_compaction(
    tmp_path, boundary_index, expected_count
):
    store = _store(tmp_path)
    source = _source_with_three_turns(store)
    source_before = store._path(source.id).read_bytes()
    boundary = source.message_ledger[boundary_index * 2].id
    key_hash, request_hash = _fork_hashes(source.id, boundary, f"key-{boundary_index}")

    forked, created = store.fork_session(source.id, boundary, key_hash, request_hash)

    assert created is True
    assert len(forked.message_ledger) == expected_count
    assert forked.compaction_checkpoint is None
    assert "boundary-after-secret" not in json.dumps(forked.to_dict())
    assert {item.id for item in forked.message_ledger}.isdisjoint(
        item.id for item in source.message_ledger
    )
    assert [item.content for item in forked.message_ledger] == [
        item.content for item in source.message_ledger[:expected_count]
    ]
    for message in forked.message_ledger:
        if message.role == "assistant":
            assert message.reply_to_message_id in {
                item.id for item in forked.message_ledger if item.role == "user"
            }
    assert store._path(source.id).read_bytes() == source_before


def test_fork_deep_copies_artifact_and_rebinds_public_identity(tmp_path):
    store = _store(tmp_path)
    source = store.new_session(provider="fake", model="openai/fake")
    spec = ChartSpecV1(
        chart_type="bar",
        title="数量",
        columns=(
            ChartColumn(key="name", label="名称", data_type="string"),
            ChartColumn(key="value", label="数量", data_type="number"),
        ),
        rows=(("A", 1),),
        x_key="name",
        series=(ChartSeries(key="value", label="数量"),),
    )
    artifact = build_chart_artifact(
        spec,
        session_id=source.id,
        run_id="run-source",
        call_id="call-source",
        created_at="2026-01-01T00:01:00Z",
    )
    user_id = "msg_aaaaaaaaaaaaaaaaaaaaaaaa"
    next_user_id = "msg_bbbbbbbbbbbbbbbbbbbbbbbb"
    source.messages = [
        {"role": "user", "content": "chart"},
        {"role": "assistant", "content": "ready"},
        {"role": "user", "content": "next"},
    ]
    source.message_ledger = [
        PublicMessageSnapshot(id=user_id, role="user", content="chart"),
        PublicMessageSnapshot(
            id=artifact.message_id,
            role="assistant",
            reply_to_message_id=user_id,
            content="ready",
            artifacts=(artifact.ref,),
        ),
        PublicMessageSnapshot(id=next_user_id, role="user", content="next"),
    ]
    source.presentations = [artifact]
    store.save(source, must_exist=False)
    source_before = store.load(source.id)
    key_hash, request_hash = _fork_hashes(source.id, next_user_id, "artifact-key")

    forked, _ = store.fork_session(source.id, next_user_id, key_hash, request_hash)

    assert len(forked.presentations) == 1
    cloned = forked.presentations[0]
    assert cloned.artifact_id != artifact.artifact_id
    assert cloned.content_hash == artifact.content_hash
    assert cloned.spec == artifact.spec
    assert cloned.session_id == forked.id
    assert cloned.run_id is None
    assert cloned.message_id == forked.message_ledger[1].id
    assert cloned.created_at == forked.created_at
    assert forked.message_ledger[1].artifacts == (cloned.ref,)
    assert store.load(source.id) == source_before


def test_compacted_model_history_cannot_rewrite_or_leak_public_ledger(tmp_path):
    store = _store(tmp_path)
    source = _source_with_three_turns(store)
    original_ledger = tuple(source.message_ledger)
    boundary = source.message_ledger[4].id
    source.messages = [
        {"role": "system", "content": "summary contains post-boundary-secret"},
        {"role": "user", "content": "user-2"},
    ]
    store.save(source)
    compacted = store.load(source.id)
    assert tuple(compacted.message_ledger) == original_ledger
    key_hash, request_hash = _fork_hashes(source.id, boundary, "compacted-key")

    forked, _ = store.fork_session(source.id, boundary, key_hash, request_hash)

    assert [item.content for item in forked.message_ledger] == [
        item.content for item in original_ledger[:4]
    ]
    assert "post-boundary-secret" not in json.dumps(forked.to_dict())


def test_fork_idempotency_survives_store_restart_and_rejects_changed_request(tmp_path):
    store = _store(tmp_path)
    source = _source_with_three_turns(store)
    first_boundary = source.message_ledger[2].id
    key_hash, request_hash = _fork_hashes(source.id, first_boundary, "stable-key")
    first, created = store.fork_session(source.id, first_boundary, key_hash, request_hash)
    restarted = _store(tmp_path)

    replay, replay_created = restarted.fork_session(
        source.id, first_boundary, key_hash, request_hash
    )
    assert created is True
    assert replay_created is False
    assert replay == first

    other_boundary = source.message_ledger[4].id
    _, changed_request_hash = _fork_hashes(source.id, other_boundary, "stable-key")
    with pytest.raises(IdempotencyConflictError):
        restarted.fork_session(source.id, other_boundary, key_hash, changed_request_hash)


def test_fork_publish_failure_leaves_no_visible_target_or_idempotency_record(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source = _source_with_three_turns(store)
    boundary = source.message_ledger[2].id
    key_hash, request_hash = _fork_hashes(source.id, boundary, "failure-key")
    original_write = store._atomic_write_locked

    def fail_target(session):
        if session.id != source.id:
            raise OSError("publish failed")
        original_write(session)

    monkeypatch.setattr(store, "_atomic_write_locked", fail_target)
    with pytest.raises(OSError, match="publish failed"):
        store.fork_session(source.id, boundary, key_hash, request_hash)

    documents = list((tmp_path / "sessions").glob("*.json"))
    assert documents == [store._path(source.id)]


def test_concurrent_same_key_publishes_one_fork(tmp_path):
    store = _store(tmp_path)
    source = _source_with_three_turns(store)
    boundary = source.message_ledger[2].id
    key_hash, request_hash = _fork_hashes(source.id, boundary, "concurrent-key")
    barrier = threading.Barrier(3)
    outcomes = []
    errors = []

    def fork() -> None:
        try:
            barrier.wait(timeout=5)
            outcomes.append(store.fork_session(source.id, boundary, key_hash, request_hash))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=fork) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    assert {item[0].id for item in outcomes} == {outcomes[0][0].id}
    assert sorted(item[1] for item in outcomes) == [False, True]
    assert len(store.list()) == 2


def test_matching_corrupt_idempotency_result_fails_closed(tmp_path):
    store = _store(tmp_path)
    source = _source_with_three_turns(store)
    boundary = source.message_ledger[2].id
    key_hash, request_hash = _fork_hashes(source.id, boundary, "corrupt-key")
    corrupt = store._path("corrupt-fork")
    corrupt.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "corrupt-fork",
                "fork_origin": {
                    "source_session_id": source.id,
                    "before_user_message_id": boundary,
                    "key_hash": key_hash,
                    "request_hash": request_hash,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SessionMigrationRequiredError):
        store.fork_session(source.id, boundary, key_hash, request_hash)
    assert len(list((tmp_path / "sessions").glob("*.json"))) == 2


def test_bound_runtime_fork_is_restart_idempotent_and_cross_session_safe(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    service = AgentService(config_path=config, workspace_root=tmp_path)
    runtime = service.create_session()
    other = service.create_session()
    try:
        assert list(runtime.start_run("first").events)[-1].terminal_status == "completed"
        snapshot = runtime.snapshot()
        boundary = snapshot.messages[0].id
        assistant_id = snapshot.messages[1].id
        with pytest.raises(InvalidForkRequestError):
            runtime.fork_session("not-a-message-id", "public-key-invalid-id")
        with pytest.raises(InvalidIdempotencyKeyError):
            runtime.fork_session(boundary, "")
        with pytest.raises(UserMessageNotFoundError):
            runtime.fork_session(assistant_id, "assistant-boundary-key")
        forked = runtime.fork_session(boundary, "public-key")
        assert forked.fork_created is True
        assert forked.messages == ()
        with pytest.raises(UserMessageNotFoundError):
            other.fork_session(boundary, "cross-session-key")
        source_id = runtime.session.id
    finally:
        runtime.close()
        other.close()

    restarted = service.load_session(source_id)
    try:
        replay = restarted.fork_session(boundary, "public-key")
        assert replay.id == forked.id
        assert replay.fork_created is False
    finally:
        restarted.close()
