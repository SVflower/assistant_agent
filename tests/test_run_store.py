"""M10b 双槽 RunStore 测试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from assistant_agent.persistence.run_store import RunStore


def _document(run_id: str, *, updated: str = "1", status: str = "running") -> dict:
    return {
        "run_id": run_id,
        "task": f"task {run_id}",
        "status": status,
        "phase": "terminal" if status in {"completed", "failed"} else "model_pending",
        "session_id": None,
        "updated_at": updated,
        "session_synced": status in {"completed", "failed"},
    }


def _session_document(
    run_id: str,
    session_id: str = "session-1",
    *,
    updated: str = "2026-01-01T00:00:00Z",
    status: str = "running",
) -> dict:
    document = _document(run_id, updated=updated, status=status)
    document["session_id"] = session_id
    return document


def _active_index(store: RunStore) -> tuple[dict, Path]:
    manifest = json.loads(store._manifest_path.read_text(encoding="ascii"))
    return manifest, store._session_index / manifest["generation"]


def test_save_load_and_previous_slot(tmp_path):
    store = RunStore(tmp_path)
    store.save("run-1", _document("run-1", updated="1"))
    store.save("run-1", _document("run-1", updated="2"))

    loaded = store.load("run-1")
    assert loaded.source == "current"
    assert loaded.document["updated_at"] == "2"
    previous = json.loads((tmp_path / "run-1.prev.json").read_text(encoding="utf-8"))
    assert previous["updated_at"] == "1"


def test_corrupt_current_falls_back_to_previous(tmp_path):
    store = RunStore(tmp_path)
    store.save("run-1", _document("run-1", updated="1"))
    store.save("run-1", _document("run-1", updated="2"))
    (tmp_path / "run-1.json").write_text("{broken", encoding="utf-8")

    loaded = store.load("run-1")
    assert loaded.source == "previous"
    assert loaded.document["updated_at"] == "1"
    assert "回退" in loaded.warning


def test_both_slots_corrupt_fails(tmp_path):
    store = RunStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "run-1.json").write_text("bad", encoding="utf-8")
    (tmp_path / "run-1.prev.json").write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="两个槽均损坏"):
        store.load("run-1")


@pytest.mark.parametrize("run_id", ["../escape", "x/y", "", ".hidden", "a" * 129])
def test_run_id_path_escape_is_rejected(tmp_path, run_id):
    with pytest.raises(ValueError, match="非法 Run ID"):
        RunStore(tmp_path).load(run_id)


def test_save_rejects_id_mismatch_and_non_json(tmp_path):
    store = RunStore(tmp_path)
    with pytest.raises(ValueError, match="ID"):
        store.save("run-1", _document("run-2"))
    bad = _document("run-1")
    bad["value"] = float("nan")
    with pytest.raises(ValueError):
        store.save("run-1", bad)


def test_save_rejects_lone_surrogate(tmp_path):
    document = _document("run-1")
    document["task"] = "\ud800"
    with pytest.raises(UnicodeEncodeError):
        RunStore(tmp_path).save("run-1", document)


def test_filename_id_mismatch_is_rejected(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "run-1.json").write_text(json.dumps(_document("run-2")), encoding="utf-8")
    with pytest.raises(ValueError, match="没有 previous"):
        RunStore(tmp_path).load("run-1")


def test_list_uses_fallback_and_sorts(tmp_path):
    store = RunStore(tmp_path)
    store.save("run-1", _document("run-1", updated="2026-01-01T00:00:01Z"))
    store.save("run-1", _document("run-1", updated="2026-01-01T00:00:02Z"))
    (tmp_path / "run-1.json").write_text("bad", encoding="utf-8")
    store.save("run-2", _document("run-2", updated="2026-01-01T00:00:03Z"))

    assert [item.id for item in store.list()] == ["run-2", "run-1"]


def test_list_includes_run_when_only_previous_slot_remains(tmp_path):
    store = RunStore(tmp_path)
    store.save("run-1", _document("run-1", updated="2026-01-01T00:00:01Z"))
    (tmp_path / "run-1.json").replace(tmp_path / "run-1.prev.json")
    assert [item.id for item in store.list()] == ["run-1"]


def test_legacy_run_files_build_persistent_session_index(tmp_path):
    document = _document("run-1", updated="2026-01-01T00:00:01Z")
    document["session_id"] = "session-1"
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "run-1.json").write_text(json.dumps(document), encoding="utf-8")

    store = RunStore(tmp_path)
    with store._lifecycle.lock("session-1"):
        last = store.last_for_session_locked("session-1")
    assert last is not None and last.id == "run-1"
    manifest = json.loads(
        (tmp_path / ".session-index-v1" / "manifest.json").read_text(encoding="ascii")
    )
    assert (
        tmp_path / ".session-index-v1" / manifest["generation"] / "session-1" / "run-1.ref"
    ).is_file()


def test_save_after_fallback_does_not_rotate_corrupt_current(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    store.save("run-1", _document("run-1", updated="1"))
    store.save("run-1", _document("run-1", updated="2"))
    (tmp_path / "run-1.json").write_text("bad", encoding="utf-8")

    real_replace = __import__("os").replace

    def fail_new_current(source, target):
        if str(target).endswith("run-1.json"):
            raise OSError("replace failed")
        return real_replace(source, target)

    monkeypatch.setattr("assistant_agent.persistence.run_store.os.replace", fail_new_current)
    with pytest.raises(OSError, match="replace failed"):
        store.save("run-1", _document("run-1", updated="3"))

    loaded = store.load("run-1")
    assert loaded.source == "previous"
    assert loaded.document["updated_at"] == "1"


def test_delete_removes_both_slots(tmp_path):
    store = RunStore(tmp_path)
    store.save("run-1", _document("run-1", updated="1"))
    store.save("run-1", _document("run-1", updated="2"))
    assert store.delete("run-1")
    assert not store.delete("run-1")
    assert not list(tmp_path.glob("run-1*"))


def test_run_tombstone_blocks_late_save_and_survives_restart(tmp_path):
    store = RunStore(tmp_path)
    stale = _document("run-1", updated="2026-01-01T00:00:01Z")
    store.save("run-1", stale)
    store.save("run-1", _document("run-1", updated="2026-01-01T00:00:02Z"))

    assert store.delete("run-1") is True
    assert store.delete("run-1") is False
    with pytest.raises(FileNotFoundError, match="Run 已删除"):
        store.save("run-1", stale)

    restarted = RunStore(tmp_path)
    with pytest.raises(FileNotFoundError, match="Run 不存在"):
        restarted.load("run-1")
    with pytest.raises(FileNotFoundError, match="Run 已删除"):
        restarted.save("run-1", stale)
    assert restarted.list() == []
    assert not list(tmp_path.glob("run-1*.json"))


def test_prune_only_synced_terminal_runs(tmp_path):
    store = RunStore(tmp_path)
    store.save("old", _document("old", updated="2026-01-01T00:00:01Z", status="completed"))
    store.save("new", _document("new", updated="2026-01-01T00:00:02Z", status="failed"))
    store.save("active", _document("active", updated="2026-01-01T00:00:03Z"))
    unsynced = _document("unsynced", updated="2026-01-01T00:00:04Z", status="completed")
    unsynced["session_synced"] = False
    store.save("unsynced", unsynced)

    assert store.prune(1) == ["old"]
    assert {item.id for item in store.list()} == {"new", "active", "unsynced"}


@pytest.mark.parametrize("damage", ["missing_ref", "missing_directory", "bad_ref", "bad_manifest"])
def test_direct_index_detects_damage_and_rebuilds_atomically(tmp_path, damage):
    store = RunStore(tmp_path)
    store.save("run-old", _session_document("run-old", updated="2026-01-01T00:00:01Z"))
    store.save("run-new", _session_document("run-new", updated="2026-01-01T00:00:02Z"))
    manifest, generation = _active_index(store)
    session_dir = generation / "session-1"
    if damage == "missing_ref":
        (session_dir / "run-new.ref").unlink()
    elif damage == "missing_directory":
        shutil.rmtree(session_dir)
    elif damage == "bad_ref":
        (session_dir / "run-new.ref").write_text("{broken", encoding="ascii")
    else:
        store._manifest_path.write_text("{broken", encoding="ascii")

    with store._lifecycle.lock("session-1"):
        last = store.last_for_session_locked("session-1")

    assert last is not None and last.id == "run-new"
    repaired, repaired_generation = _active_index(store)
    assert repaired["sessions"] == {"session-1": ["run-new", "run-old"]}
    assert repaired_generation != generation
    assert not generation.exists()


def test_index_restart_repairs_damage_and_cleans_crash_temporaries(tmp_path):
    store = RunStore(tmp_path)
    store.save("run-1", _session_document("run-1"))
    manifest, generation = _active_index(store)
    shutil.rmtree(generation / "session-1")
    (store._session_index / ".manifest-crash.tmp").write_text("partial", encoding="ascii")
    orphan = store._session_index / "g-orphan"
    orphan.mkdir()
    (orphan / ".ref-crash.tmp").write_text("partial", encoding="ascii")

    restarted = RunStore(tmp_path)
    with restarted._lifecycle.lock("session-1"):
        last = restarted.last_for_session_locked("session-1")

    assert last is not None and last.id == "run-1"
    repaired, repaired_generation = _active_index(restarted)
    assert repaired["sessions"] == {"session-1": ["run-1"]}
    assert {path.name for path in restarted._session_index.iterdir()} == {
        "manifest.json",
        repaired["generation"],
    }
    assert not list(repaired_generation.rglob("*.tmp"))


def test_last_for_session_uses_public_status_filter_before_latest_selection(tmp_path):
    store = RunStore(tmp_path)
    cases = [
        ("run-completed", "2026-01-01T00:00:01Z", "completed"),
        ("run-running", "2026-01-01T00:00:02Z", "running"),
        ("run-paused", "2026-01-01T00:00:03Z", "paused"),
        ("run-failed-a", "2026-01-01T00:00:04Z", "failed"),
        ("run-failed-z", "2026-01-01T00:00:04Z", "failed"),
        ("run-polluted", "2027-01-01T00:00:00Z", "future-state"),
    ]
    for run_id, updated, status in cases:
        store.save(run_id, _session_document(run_id, updated=updated, status=status))

    with store._lifecycle.lock("session-1"):
        last = store.last_for_session_locked("session-1")

    assert last is not None
    assert (last.id, last.status) == ("run-failed-z", "failed")


def test_delete_prune_and_session_cascade_remove_refs_without_weakening_tombstones(tmp_path):
    lifecycle = tmp_path / "session-lifecycle"
    store = RunStore(tmp_path / "runs", lifecycle_dir=lifecycle)
    store.save("single", _session_document("single", "session-single"))
    assert store.delete("single") is True
    assert store.delete("single") is False
    with pytest.raises(FileNotFoundError, match="Run 已删除"):
        store.save("single", _session_document("single", "session-single"))

    store.save(
        "pruned",
        _session_document("pruned", "session-prune", status="completed"),
    )
    assert store.prune(0) == ["pruned"]

    store.save("cascade-a", _session_document("cascade-a", "session-cascade"))
    store.save("cascade-b", _session_document("cascade-b", "session-cascade"))
    with store._lifecycle.lock("session-cascade"):
        store._lifecycle.mark_deleted_locked("session-cascade")
    assert store.delete_session_runs("session-cascade") == ["cascade-a", "cascade-b"]

    manifest, generation = _active_index(store)
    assert manifest["sessions"] == {}
    assert not [path for path in generation.iterdir() if path.is_dir()]
    for run_id in ("single", "pruned", "cascade-a", "cascade-b"):
        assert store._run_lifecycle.is_deleted(run_id)


def test_many_create_delete_cycles_leave_bounded_index_and_index_lock_files(tmp_path):
    store = RunStore(tmp_path)
    for number in range(75):
        run_id = f"run-{number}"
        store.save(run_id, _session_document(run_id, f"session-{number}"))
        assert store.delete(run_id)

    manifest, generation = _active_index(store)
    assert manifest["sessions"] == {}
    assert list(generation.iterdir()) == []
    assert {path.name for path in store._session_index.iterdir()} == {
        "manifest.json",
        manifest["generation"],
    }
    assert len(list(store._index_lifecycle._dir.glob("*.lock"))) == 1
    assert len(list(store._run_lifecycle._dir.glob("*.lock"))) <= 64
    assert len(list(store._lifecycle._dir.glob("*.lock"))) <= 64
    assert len(list(store._run_lifecycle._dir.glob("*.deleted"))) == 75
