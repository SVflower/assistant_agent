"""M10b 双槽 RunStore 测试。"""

from __future__ import annotations

import json

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
    store.save("run-1", _document("run-1", updated="1"))
    store.save("run-1", _document("run-1", updated="2"))
    (tmp_path / "run-1.json").write_text("bad", encoding="utf-8")
    store.save("run-2", _document("run-2", updated="3"))

    assert [item.id for item in store.list()] == ["run-2", "run-1"]


def test_list_includes_run_when_only_previous_slot_remains(tmp_path):
    store = RunStore(tmp_path)
    store.save("run-1", _document("run-1", updated="1"))
    (tmp_path / "run-1.json").replace(tmp_path / "run-1.prev.json")
    assert [item.id for item in store.list()] == ["run-1"]


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


def test_prune_only_synced_terminal_runs(tmp_path):
    store = RunStore(tmp_path)
    store.save("old", _document("old", updated="1", status="completed"))
    store.save("new", _document("new", updated="2", status="failed"))
    store.save("active", _document("active", updated="3"))
    unsynced = _document("unsynced", updated="4", status="completed")
    unsynced["session_synced"] = False
    store.save("unsynced", unsynced)

    assert store.prune(1) == ["old"]
    assert {item.id for item in store.list()} == {"new", "active", "unsynced"}
