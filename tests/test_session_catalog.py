"""M23-R1 Session schema、catalog、CAS 与并发测试。"""

from __future__ import annotations

import base64
import json
import subprocess
import sys

import pytest

from assistant_agent.application.models import RunMeta
from assistant_agent.application.sessions import AgentService
from assistant_agent.contracts.errors import (
    InvalidSessionCursorError,
    InvalidSessionLimitError,
    InvalidSessionMetadataError,
    InvalidSessionQueryError,
    SessionMetadataConflictError,
    SessionUnavailableError,
)
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore, UnsupportedSessionSchemaError


def _service(tmp_path, *, run_store=None):
    return AgentService(
        runtime_factory=lambda *_args: None,  # type: ignore[arg-type]
        session_store=SessionStore(tmp_path / "sessions"),
        run_store=run_store or RunStore(tmp_path / "runs"),
        max_completed_runs=10,
    )


def test_legacy_session_migrates_once_without_changing_messages(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    path = store._path("legacy")
    path.parent.mkdir(parents=True)
    messages = [
        {"role": "system", "content": "private"},
        {"role": "user", "content": "  第一条\n\t公开问题  "},
    ]
    path.write_text(
        json.dumps(
            {
                "id": "legacy",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-02T00:00:00",
                "messages": messages,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = store.load("legacy")
    second = store.load("legacy")
    assert first == second
    assert first.schema_version == 1
    assert first.title == "第一条 公开问题"
    assert first.title_source == "auto"
    assert first.metadata_version == 1
    assert first.messages == messages
    summary = _service(tmp_path).catalog_sessions().items[0]
    assert summary.created_at.endswith("Z")
    assert summary.updated_at.endswith("Z")


def test_unknown_future_session_schema_fails_closed(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    path = store._path("future")
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":2,"id":"future"}', encoding="utf-8")
    with pytest.raises(UnsupportedSessionSchemaError):
        store.load("future")
    with pytest.raises(SessionUnavailableError):
        _service(tmp_path).catalog_sessions()


def test_auto_title_boundaries_and_user_title_is_never_overwritten(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [{"role": "user", "content": "\u2003\n\t"}])
    assert store.load(session.id).title == "（空会话）"
    source = "字" * 81
    store.save(
        session,
        [
            {"role": "user", "content": "\u2003\n\t"},
            {"role": "user", "content": f"  {source}  "},
        ],
    )
    titled = store.load(session.id)
    assert titled.title == "字" * 80
    assert titled.metadata_version == 2
    renamed = store.update_metadata(session.id, "  用户 原值  ", 2)
    store.save(session, [{"role": "user", "content": "新的自动标题候选"}])
    saved = store.load(session.id)
    assert saved.title == renamed.title == "  用户 原值  "
    assert saved.title_source == "user"
    assert saved.metadata_version == 3


def test_metadata_cas_and_stale_run_save_preserve_rename(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [])
    stale_run_snapshot = store.load(session.id)
    renamed = store.update_metadata(session.id, "renamed", 1)
    with pytest.raises(SessionMetadataConflictError) as conflict:
        store.update_metadata(session.id, "lost", 1)
    assert conflict.value.current_metadata_version == 2

    store.save(stale_run_snapshot, [{"role": "user", "content": "run task"}])
    merged = store.load(session.id)
    assert merged.title == renamed.title
    assert merged.title_source == "user"
    assert merged.metadata_version == 2
    assert merged.messages == [{"role": "user", "content": "run task"}]


def test_cross_process_metadata_cas_has_exactly_one_winner(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [])
    start = tmp_path / "start"
    script = """
import sys, time
from pathlib import Path
from assistant_agent.contracts.errors import SessionMetadataConflictError
from assistant_agent.persistence.store import SessionStore
base, start, result = map(Path, sys.argv[1:4])
deadline = time.monotonic() + 10
while not start.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
try:
    SessionStore(base).update_metadata(sys.argv[4], sys.argv[5], 1)
except SessionMetadataConflictError:
    result.write_text('conflict', encoding='ascii')
else:
    result.write_text('updated', encoding='ascii')
"""
    results = [tmp_path / f"result-{index}" for index in range(2)]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path / "sessions"),
                str(start),
                str(result),
                session.id,
                f"title-{index}",
            ]
        )
        for index, result in enumerate(results)
    ]
    try:
        start.touch()
        for process in processes:
            process.wait(timeout=10)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    outcomes = [result.read_text(encoding="ascii") for result in results]
    assert outcomes.count("updated") == 1
    assert outcomes.count("conflict") == 1
    assert all(process.returncode == 0 for process in processes)


def test_catalog_same_second_cursor_query_binding_and_tamper(tmp_path, monkeypatch):
    import assistant_agent.application.sessions as session_app
    import assistant_agent.persistence.store as store_module

    monkeypatch.setattr(store_module, "_now_iso", lambda: "2026-07-20T00:00:00Z")
    store = SessionStore(tmp_path / "sessions")
    ids = []
    for title in ("Ａlpha", "alpha two", "other"):
        session = store.new_session()
        store.save(session, [])
        store.update_metadata(session.id, title, 1)
        ids.append(session.id)
    service = _service(tmp_path)

    seen = []
    cursor = None
    while True:
        page = service.catalog_sessions(limit=1, cursor=cursor)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == sorted(ids, reverse=True)
    assert len(seen) == len(set(seen)) == 3

    first = service.catalog_sessions(query="alpha", limit=1)
    assert first.next_cursor is not None
    assert first.items[0].title in {"Ａlpha", "alpha two"}
    reused = service.catalog_sessions(query="ＡLPHA", limit=100, cursor=first.next_cursor)
    assert len(reused.items) == 1
    with pytest.raises(InvalidSessionCursorError):
        service.catalog_sessions(query="other", cursor=first.next_cursor)
    with pytest.raises(InvalidSessionCursorError):
        service.catalog_sessions(cursor=first.next_cursor[:-1] + "A")
    padding = "=" * (-len(first.next_cursor) % 4)
    envelope = json.loads(base64.urlsafe_b64decode(first.next_cursor + padding))
    envelope["data"]["version"] = 2
    data = json.dumps(
        envelope["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    envelope["signature"] = session_app._cursor_signature(data)
    unsupported = (
        base64.urlsafe_b64encode(json.dumps(envelope, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(InvalidSessionCursorError):
        service.catalog_sessions(query="alpha", cursor=unsupported)


def test_catalog_only_searches_public_preview_and_aggregates_last_run_once(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(
        session,
        [
            {"role": "system", "content": "private-needle"},
            {"role": "tool", "content": "tool-needle"},
            {"role": "assistant", "content": "public answer"},
        ],
    )

    class CountingRuns:
        def __init__(self):
            self.calls = 0

        def list(self):
            self.calls += 1
            return [
                RunMeta(
                    "run-a",
                    "completed",
                    "terminal",
                    session.id,
                    "2026-07-20T00:00:00",
                    "secret",
                ),
                RunMeta(
                    "run-b",
                    "failed",
                    "terminal",
                    session.id,
                    "2026-07-20T00:00:00",
                    "secret",
                ),
            ]

        def load(self, _run_id):
            raise AssertionError("catalog 不得逐 Run load")

    runs = CountingRuns()
    service = _service(tmp_path, run_store=runs)
    assert service.catalog_sessions(query="private-needle").items == ()
    assert service.catalog_sessions(query="tool-needle").items == ()
    page = service.catalog_sessions(query="PUBLIC ANSWER")
    assert page.items[0].message_count == 1
    assert page.items[0].last_run is not None
    assert page.items[0].last_run.id == "run-b"
    assert page.items[0].last_run.updated_at.endswith("Z")
    assert runs.calls == 3


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"query": "x" * 201}, InvalidSessionQueryError),
        ({"query": 1}, InvalidSessionQueryError),
        ({"limit": 0}, InvalidSessionLimitError),
        ({"limit": True}, InvalidSessionLimitError),
    ],
)
def test_catalog_validation_is_stable(tmp_path, kwargs, error):
    with pytest.raises(error):
        _service(tmp_path).catalog_sessions(**kwargs)


def test_update_metadata_validation_is_stable(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [])
    service = _service(tmp_path)
    updated = service.update_session_metadata(session.id, "界" * 100, 1)
    assert updated.title == "界" * 100
    for title in ("", " \u2003", "x" * 101):
        with pytest.raises(InvalidSessionMetadataError):
            service.update_session_metadata(session.id, title, 2)
    with pytest.raises(InvalidSessionMetadataError):
        service.update_session_metadata(session.id, "valid", True)
