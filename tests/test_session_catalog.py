"""M23-R1 Session schema、catalog、CAS 与并发测试。"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading

import pytest

from assistant_agent.application.models import RunMeta
from assistant_agent.application.sessions import AgentService
from assistant_agent.contracts.charts import (
    build_chart_artifact_v2,
)
from assistant_agent.contracts.errors import (
    InvalidSessionCursorError,
    InvalidSessionLimitError,
    InvalidSessionMetadataError,
    InvalidSessionQueryError,
    SessionMetadataConflictError,
    SessionNotFoundError,
    SessionUnavailableError,
    UnsupportedSessionSchemaError,
)
from assistant_agent.persistence.execution_lease import FileSessionExecutionLeaseManager
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.tools.chart_input_v2 import normalize_chart_v2_input


def _service(tmp_path, *, run_store=None, session_store=None):
    return AgentService(
        runtime_factory=lambda *_args: None,  # type: ignore[arg-type]
        session_store=session_store or SessionStore(tmp_path / "sessions"),
        run_store=run_store or RunStore(tmp_path / "runs"),
        session_leases=FileSessionExecutionLeaseManager(tmp_path / "execution-leases"),
        max_completed_runs=10,
    )


def _run_document(run_id: str, session_id: str, updated_at: str, status="completed"):
    return {
        "schema_version": 7,
        "run_id": run_id,
        "session_id": session_id,
        "task": "public summary",
        "status": status,
        "phase": "terminal" if status in {"completed", "failed", "cancelled"} else "model_pending",
        "updated_at": updated_at,
    }


def test_get_session_summary_reads_by_id_and_aggregates_last_run(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    runs = RunStore(tmp_path / "runs")
    session = store.new_session()
    store.save(
        session,
        [
            {"role": "system", "content": "private"},
            {"role": "user", "content": "public question"},
            {"role": "assistant", "content": "public answer"},
        ],
        must_exist=False,
    )
    runs.save(
        "run-older",
        _run_document("run-older", session.id, "2026-07-20T09:00:00Z"),
    )
    runs.save(
        "run-latest",
        _run_document("run-latest", session.id, "2026-07-20T10:00:00+01:00"),
    )
    runs.save(
        "run-newest",
        _run_document("run-newest", session.id, "2026-07-20T09:00:00.1Z", "failed"),
    )
    original_glob = type(runs._dir).glob

    def reject_full_run_scan(path, pattern):
        if path == runs._dir and pattern == "*.json":
            raise AssertionError("按 ID summary 不得扫描全量 Run 目录")
        return original_glob(path, pattern)

    monkeypatch.setattr(type(runs._dir), "glob", reject_full_run_scan)
    monkeypatch.setattr(
        runs, "list", lambda: (_ for _ in ()).throw(AssertionError("不得读取 Run catalog"))
    )
    monkeypatch.setattr(
        runs, "load", lambda _run_id: (_ for _ in ()).throw(AssertionError("不得逐 Run load"))
    )

    summary = _service(tmp_path, session_store=store, run_store=runs).get_session_summary(
        session.id
    )
    assert summary.id == session.id
    assert summary.title == "public question"
    assert summary.title_source == "auto"
    assert summary.metadata_version == 2
    assert summary.message_count == 2
    assert summary.preview == "public question"
    assert summary.created_at.endswith("Z")
    assert summary.updated_at.endswith("Z")
    assert summary.last_run is not None
    assert summary.last_run.id == "run-newest"
    assert summary.last_run.status == "failed"
    assert summary.last_run.updated_at == "2026-07-20T09:00:00.100000Z"


def test_repaired_direct_summary_matches_catalog_and_ignores_newer_unknown_status(tmp_path):
    sessions = SessionStore(tmp_path / "sessions")
    runs = RunStore(tmp_path / "runs")
    session = sessions.new_session()
    sessions.save(session, [{"role": "user", "content": "indexed"}], must_exist=False)
    runs.save(
        "run-valid",
        _run_document("run-valid", session.id, "2026-07-20T09:00:00Z", "paused"),
    )
    runs.save(
        "run-polluted",
        _run_document("run-polluted", session.id, "2027-07-20T09:00:00Z", "future-state"),
    )
    manifest = json.loads(runs._manifest_path.read_text(encoding="ascii"))
    ref = runs._session_index / manifest["generation"] / session.id / "run-valid.ref"
    ref.unlink()
    service = _service(tmp_path, session_store=sessions, run_store=runs)

    direct = service.get_session_summary(session.id)
    catalog = service.catalog_sessions().items[0]

    assert direct == catalog
    assert direct.last_run is not None
    assert (direct.last_run.id, direct.last_run.status) == ("run-valid", "paused")


def test_unrepairable_direct_index_failure_is_stable_unavailable(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path / "sessions")
    runs = RunStore(tmp_path / "runs")
    session = sessions.new_session()
    sessions.save(session, [], must_exist=False)
    runs.save("run-1", _run_document("run-1", session.id, "2026-07-20T09:00:00Z"))
    manifest = json.loads(runs._manifest_path.read_text(encoding="ascii"))
    ref = runs._session_index / manifest["generation"] / session.id / "run-1.ref"
    ref.unlink()
    real_replace = __import__("os").replace

    def fail_manifest_replace(source, target):
        if target == runs._manifest_path:
            raise OSError("injected index commit failure")
        return real_replace(source, target)

    monkeypatch.setattr("assistant_agent.persistence.run_store.os.replace", fail_manifest_replace)
    service = _service(tmp_path, session_store=sessions, run_store=runs)

    with pytest.raises(SessionUnavailableError, match="summary 暂不可用"):
        service.get_session_summary(session.id)


@pytest.mark.parametrize(
    ("damage", "expected_run_id"),
    [("coherent_omission", "run-new"), ("stale_ref", "run-old")],
)
def test_restart_authoritative_index_repair_keeps_direct_and_catalog_consistent(
    tmp_path, damage, expected_run_id
):
    sessions = SessionStore(tmp_path / "sessions")
    runs = RunStore(tmp_path / "runs")
    session = sessions.new_session()
    sessions.save(session, [{"role": "user", "content": "repair"}], must_exist=False)
    runs.save("run-old", _run_document("run-old", session.id, "2026-01-01T00:00:01Z"))
    runs.save("run-new", _run_document("run-new", session.id, "2026-01-01T00:00:02Z"))
    manifest = json.loads(runs._manifest_path.read_text(encoding="ascii"))
    generation = runs._session_index / manifest["generation"]
    if damage == "coherent_omission":
        manifest["sessions"][session.id].remove("run-new")
        (generation / session.id / "run-new.ref").unlink()
        runs._manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="ascii"
        )
    else:
        runs._path("run-new").unlink()
        runs._path("run-new", previous=True).unlink(missing_ok=True)

    restarted = RunStore(tmp_path / "runs")
    service = _service(tmp_path, session_store=sessions, run_store=restarted)
    direct = service.get_session_summary(session.id)
    catalog = service.catalog_sessions().items[0]

    assert direct == catalog
    assert direct.last_run is not None
    assert direct.last_run.id == expected_run_id


def test_get_session_summary_not_found_and_tombstone(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(SessionNotFoundError):
        service.get_session_summary("missing")

    store = service._session_store
    session = store.new_session()
    store.save(session, [], must_exist=False)
    assert store.delete(session.id)
    with pytest.raises(SessionNotFoundError):
        service.get_session_summary(session.id)


def test_get_session_summary_does_not_scan_catalog_or_issue_n_plus_one(tmp_path):
    session = SessionStore(tmp_path / "seed").new_session()

    class DirectSessionStore:
        def __init__(self):
            self.reads = 0

        def read_locked(self, session_id, reader):
            self.reads += 1
            assert session_id == session.id
            return reader(session)

        def load(self, _session_id):
            raise AssertionError("不得通过 load 后再做非原子聚合")

        def list(self):
            raise AssertionError("不得扫描 Session catalog")

    class DirectRunStore:
        def __init__(self):
            self.aggregations = 0

        def last_for_session_locked(self, session_id):
            self.aggregations += 1
            assert session_id == session.id
            return None

        def list(self):
            raise AssertionError("不得读取全量公共 Run catalog")

        def load(self, _run_id):
            raise AssertionError("不得产生逐 Run N+1")

    sessions = DirectSessionStore()
    runs = DirectRunStore()
    service = _service(tmp_path, session_store=sessions, run_store=runs)
    assert service.get_session_summary(session.id).id == session.id
    assert sessions.reads == 1
    assert runs.aggregations == 1


@pytest.mark.parametrize("failure_source", ["session", "run"])
def test_get_session_summary_maps_storage_failures(tmp_path, failure_source):
    session = SessionStore(tmp_path / "seed").new_session()

    class Sessions:
        def read_locked(self, _session_id, reader):
            if failure_source == "session":
                raise OSError("session unavailable")
            return reader(session)

    class Runs:
        def last_for_session_locked(self, _session_id):
            raise OSError("run unavailable")

    with pytest.raises(SessionUnavailableError):
        _service(tmp_path, session_store=Sessions(), run_store=Runs()).get_session_summary(
            session.id
        )


def test_get_session_summary_rejects_legacy_session_without_rewriting(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    path = store._path("legacy-summary")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "id": "legacy-summary",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-02T00:00:00",
                "messages": [{"role": "user", "content": "legacy title"}],
            }
        ),
        encoding="utf-8",
    )
    original = path.read_bytes()
    with pytest.raises(UnsupportedSessionSchemaError) as caught:
        _service(tmp_path, session_store=store).get_session_summary("legacy-summary")
    assert caught.value.code == "unsupported_session_schema"
    assert path.read_bytes() == original


def test_get_session_summary_linearizes_before_concurrent_rename(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    runs = RunStore(tmp_path / "runs")
    session = store.new_session()
    store.save(session, [], must_exist=False)
    entered = threading.Event()
    release = threading.Event()
    original_last = runs.last_for_session_locked

    def blocking_last(session_id):
        entered.set()
        assert release.wait(timeout=5)
        return original_last(session_id)

    runs.last_for_session_locked = blocking_last  # type: ignore[method-assign]
    service = _service(tmp_path, session_store=store, run_store=runs)
    summaries = []
    summary_thread = threading.Thread(
        target=lambda: summaries.append(service.get_session_summary(session.id))
    )
    renamed = []
    rename_thread = threading.Thread(
        target=lambda: renamed.append(store.update_metadata(session.id, "renamed", 1))
    )
    summary_thread.start()
    assert entered.wait(timeout=5)
    rename_thread.start()
    assert rename_thread.is_alive()
    release.set()
    summary_thread.join(timeout=5)
    rename_thread.join(timeout=5)
    assert not summary_thread.is_alive() and not rename_thread.is_alive()
    assert summaries[0].title == "（空会话）"
    assert renamed[0].title == "renamed"
    assert service.get_session_summary(session.id).title == "renamed"


def test_get_session_summary_linearizes_before_concurrent_delete(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    runs = RunStore(tmp_path / "runs")
    session = store.new_session()
    store.save(session, [], must_exist=False)
    entered = threading.Event()
    release = threading.Event()

    def blocking_last(_session_id):
        entered.set()
        assert release.wait(timeout=5)
        return None

    runs.last_for_session_locked = blocking_last  # type: ignore[method-assign]
    service = _service(tmp_path, session_store=store, run_store=runs)
    summaries = []
    summary_thread = threading.Thread(
        target=lambda: summaries.append(service.get_session_summary(session.id))
    )
    deleted = []
    delete_thread = threading.Thread(target=lambda: deleted.append(store.delete(session.id)))
    summary_thread.start()
    assert entered.wait(timeout=5)
    delete_thread.start()
    assert delete_thread.is_alive()
    release.set()
    summary_thread.join(timeout=5)
    delete_thread.join(timeout=5)
    assert not summary_thread.is_alive() and not delete_thread.is_alive()
    assert summaries[0].id == session.id
    assert deleted == [True]
    with pytest.raises(SessionNotFoundError):
        service.get_session_summary(session.id)


def test_unknown_future_session_schema_fails_closed(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    path = store._path("future")
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":4,"id":"future"}', encoding="utf-8")
    with pytest.raises(UnsupportedSessionSchemaError) as caught:
        store.load("future")
    assert caught.value.code == "unsupported_session_schema"
    assert caught.value.expected_version == 3
    assert caught.value.actual_version == 4
    with pytest.raises(UnsupportedSessionSchemaError):
        _service(tmp_path).catalog_sessions()


def test_catalog_fails_closed_on_v1_session_without_rewriting_it(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    healthy = store.new_session()
    store.save(healthy, [{"role": "user", "content": "healthy"}], must_exist=False)

    bad_path = store._path("bad-v1-chart")
    bad_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "bad-v1-chart",
                "title": "bad chart",
                "title_source": "auto",
                "metadata_version": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "messages": [
                    {"role": "user", "content": "chart"},
                    {"role": "assistant", "content": "ready"},
                ],
                "assistant_messages": [],
                "presentations": [],
                "message_ledger": [],
                "fork_origin": None,
            }
        ),
        encoding="utf-8",
    )
    original = bad_path.read_bytes()

    with pytest.raises(UnsupportedSessionSchemaError):
        _service(tmp_path, session_store=store).catalog_sessions()
    assert bad_path.read_bytes() == original
    with pytest.raises(UnsupportedSessionSchemaError):
        store.load("bad-v1-chart")
    assert bad_path.read_bytes() == original


def test_current_session_chart_artifact_roundtrip_is_unchanged(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    spec = normalize_chart_v2_input(
        {
            "schema_version": 2,
            "chart_type": "line",
            "title": "当前图表",
            "columns": [
                {"key": "name", "label": "名称"},
                {"key": "value", "label": "数量"},
            ],
            "rows": [["A", 1]],
            "x_key": "name",
            "series": [{"key": "value", "label": "数量"}],
        }
    )
    artifact = build_chart_artifact_v2(
        spec,
        session_id=session.id,
        run_id="run-current-chart",
        call_id="call-current-chart",
        created_at="2026-01-01T00:01:00Z",
    )
    session.presentations = [artifact]
    store.save(session, [{"role": "user", "content": "current"}], must_exist=False)
    saved = store._path(session.id).read_bytes()

    loaded = store.load(session.id)

    assert loaded.presentations == [artifact]
    assert store._path(session.id).read_bytes() == saved


def test_auto_title_boundaries_and_user_title_is_never_overwritten(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [{"role": "user", "content": "\u2003\n\t"}], must_exist=False)
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
    store.save(session, [], must_exist=False)
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
    store.save(session, [], must_exist=False)
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
        store.save(session, [], must_exist=False)
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


def test_catalog_mixed_time_formats_use_utc_keyset_order(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    documents = (
        ("naive", "2026-01-01T10:00:00"),
        ("offset", "2026-01-01T11:00:00+02:00"),
        ("zulu", "2026-01-01T09:30:00Z"),
    )
    for session_id, updated_at in documents:
        path = store._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "id": session_id,
                    "title": session_id,
                    "title_source": "auto",
                    "metadata_version": 1,
                    "created_at": updated_at,
                    "updated_at": updated_at,
                    "messages": [],
                    "message_ledger": [],
                }
            ),
            encoding="utf-8",
        )
    service = _service(tmp_path)
    seen = []
    cursor = None
    while True:
        page = service.catalog_sessions(limit=1, cursor=cursor)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == ["naive", "zulu", "offset"]
    assert all(item.updated_at.endswith("Z") for item in service.catalog_sessions().items)


def test_catalog_fractional_instants_remain_distinct_across_cursor_pages(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    documents = (
        ("fraction-low", "2026-01-01T00:00:00.1"),
        ("fraction-high", "2026-01-01T01:00:00.9+01:00"),
        ("fraction-mid", "2026-01-01T00:00:00.5Z"),
    )
    for session_id, updated_at in documents:
        path = store._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "id": session_id,
                    "title": session_id,
                    "title_source": "auto",
                    "metadata_version": 1,
                    "created_at": updated_at,
                    "updated_at": updated_at,
                    "messages": [],
                    "message_ledger": [],
                }
            ),
            encoding="utf-8",
        )

    service = _service(tmp_path)
    seen = []
    cursor = None
    while True:
        page = service.catalog_sessions(limit=1, cursor=cursor)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == ["fraction-high", "fraction-mid", "fraction-low"]


def test_catalog_only_searches_public_preview_and_aggregates_last_run_once(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(
        session,
        [
            {"role": "system", "content": "private-needle"},
            {"role": "tool", "content": "tool-needle"},
            {"role": "user", "content": "public question"},
            {"role": "assistant", "content": "public answer"},
        ],
        must_exist=False,
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
                    "2026-07-20T10:00:00Z",
                    "secret",
                ),
                RunMeta(
                    "run-b",
                    "failed",
                    "terminal",
                    session.id,
                    "2026-07-20T12:00:00+02:00",
                    "secret",
                ),
                RunMeta(
                    "run-c",
                    "completed",
                    "terminal",
                    session.id,
                    "2026-07-20T09:30:00",
                    "secret",
                ),
            ]

        def load(self, _run_id):
            raise AssertionError("catalog 不得逐 Run load")

    runs = CountingRuns()
    service = _service(tmp_path, run_store=runs)
    assert service.catalog_sessions(query="private-needle").items == ()
    assert service.catalog_sessions(query="tool-needle").items == ()
    page = service.catalog_sessions(query="PUBLIC QUESTION")
    assert page.items[0].message_count == 2
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
    store.save(session, [], must_exist=False)
    service = _service(tmp_path)
    updated = service.update_session_metadata(session.id, "界" * 100, 1)
    assert updated.title == "界" * 100
    for title in ("", " \u2003", "x" * 101):
        with pytest.raises(InvalidSessionMetadataError):
            service.update_session_metadata(session.id, title, 2)
    with pytest.raises(InvalidSessionMetadataError):
        service.update_session_metadata(session.id, "valid", True)


def test_metadata_update_reads_run_catalog_before_commit(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [], must_exist=False)

    class FailingRuns:
        def list(self):
            raise OSError("run catalog unavailable")

    service = _service(tmp_path, run_store=FailingRuns())
    with pytest.raises(SessionUnavailableError):
        service.update_session_metadata(session.id, "must not commit", 1)
    unchanged = store.load(session.id)
    assert unchanged.title == "（空会话）"
    assert unchanged.metadata_version == 1


def test_metadata_update_has_no_fallible_run_read_after_commit(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [], must_exist=False)

    class OneReadRuns:
        def __init__(self):
            self.calls = 0

        def list(self):
            self.calls += 1
            if self.calls > 1:
                raise OSError("post-commit read must not happen")
            return []

    runs = OneReadRuns()
    service = _service(tmp_path, run_store=runs)
    updated = service.update_session_metadata(session.id, "committed", 1)
    assert updated.title == "committed"
    assert updated.metadata_version == 2
    assert runs.calls == 1
    assert store.load(session.id).title == "committed"


def test_metadata_update_maps_session_store_failure_without_commit(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session = store.new_session()
    store.save(session, [], must_exist=False)
    service = _service(tmp_path)

    def fail_update(*_args, **_kwargs):
        raise OSError("session store unavailable")

    monkeypatch.setattr(service._session_store, "update_metadata", fail_update)
    with pytest.raises(SessionUnavailableError):
        service.update_session_metadata(session.id, "must not commit", 1)
    unchanged = store.load(session.id)
    assert unchanged.title == "（空会话）"
    assert unchanged.metadata_version == 1
