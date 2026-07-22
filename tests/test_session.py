"""会话持久化（SessionStore）测试。"""

from __future__ import annotations

import json
import os
import re

import pytest

from assistant_agent.persistence.store import Session, SessionStore, new_session_id


def _store(tmp_path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def test_new_session_id_unique_and_sortable():
    ids = {new_session_id() for _ in range(20)}
    assert len(ids) == 20  # 不撞
    assert all(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", session_id) for session_id in ids)


def test_save_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    s = store.new_session(provider="cloud", model="openai/deepseek-v4-pro")
    msgs = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你？"},
    ]
    store.save(s, msgs, must_exist=False)

    loaded = store.load(s.id)
    assert loaded.id == s.id
    assert loaded.provider == "cloud"
    assert loaded.messages == msgs


def test_load_missing_raises(tmp_path):
    store = _store(tmp_path)
    try:
        store.load("nonexistent")
        raise AssertionError("应抛 FileNotFoundError")
    except FileNotFoundError:
        pass


def test_save_survives_surrogate_chars(tmp_path):
    """内容含孤代理（如失败模型输出/错误串）时，保存不得崩溃。"""
    store = _store(tmp_path)
    s = store.new_session()
    # \udce8 是孤代理，utf-8 直接编码会抛 UnicodeEncodeError
    store.save(
        s,
        [
            {"role": "user", "content": "测试"},
            {"role": "assistant", "content": "坏字符\udce8结尾"},
        ],
        must_exist=False,
    )
    # 不崩即通过；文件应已写出
    assert store._path(s.id).exists()


def test_list_sorted_recent_first(tmp_path):
    store = _store(tmp_path)
    s1 = store.new_session()
    s1.created_at = "2026-07-01T10:00:00"
    store.save(s1, [{"role": "user", "content": "第一个"}], must_exist=False)
    s2 = store.new_session()
    store.save(s2, [{"role": "user", "content": "第二个"}], must_exist=False)

    metas = store.list()
    assert len(metas) == 2
    # 最近更新的在前
    assert metas[0].updated_at >= metas[1].updated_at
    assert all(m.message_count == 1 for m in metas)


def test_list_preview_is_first_user_message(tmp_path):
    store = _store(tmp_path)
    s = store.new_session()
    store.save(
        s,
        [
            {"role": "user", "content": "帮我写个函数"},
            {"role": "assistant", "content": "好"},
        ],
        must_exist=False,
    )
    meta = store.list()[0]
    assert "帮我写个函数" in meta.preview


def test_delete(tmp_path):
    store = _store(tmp_path)
    s = store.new_session()
    store.save(s, [{"role": "user", "content": "x"}], must_exist=False)
    assert store.delete(s.id) is True
    assert store.delete(s.id) is False  # 再删返回 False
    assert store.list() == []


def test_list_skips_corrupt_files(tmp_path):
    store = _store(tmp_path)
    s = store.new_session()
    store.save(s, [{"role": "user", "content": "ok"}], must_exist=False)
    # 写一个损坏的 json，list 应跳过而非崩溃
    (tmp_path / "sessions" / "broken.json").write_text("{not json", encoding="utf-8")
    metas = store.list()
    assert len(metas) == 1
    assert metas[0].id == s.id


def test_session_from_dict_roundtrip():
    s = Session(
        id="x",
        created_at="a",
        updated_at="b",
        provider="p",
        model="m",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert Session.from_dict(s.to_dict()) == s


@pytest.mark.parametrize("session_id", ["../outside", "..\\outside", "/tmp/outside", "a/b"])
def test_session_id_cannot_escape_store(tmp_path, session_id):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="会话 ID"):
        store._path(session_id)


def test_atomic_save_failure_preserves_old_file(tmp_path, monkeypatch):
    store = _store(tmp_path)
    session = store.new_session()
    store.save(session, [{"role": "user", "content": "old"}], must_exist=False)

    def fail_replace(_source, _target):
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        store.save(
            session,
            [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "new"},
            ],
        )

    assert store.load(session.id).messages[0]["content"] == "old"
    assert not list((tmp_path / "sessions").glob("*.tmp"))


@pytest.mark.parametrize("version", [None, 0])
def test_v0_session_migrates_atomically_and_repeated_load_is_stable(tmp_path, version):
    store = _store(tmp_path)
    path = store._path("legacy-v0")
    path.parent.mkdir(parents=True)
    document = {
        "id": "legacy-v0",
        "created_at": "2026-01-01T08:00:00+08:00",
        "updated_at": "2026-01-02T00:00:00",
        "messages": [{"role": "user", "content": "legacy title"}],
    }
    if version is not None:
        document["schema_version"] = version
    path.write_text(json.dumps(document), encoding="utf-8")

    first = store.load("legacy-v0")
    first_bytes = path.read_bytes()
    second = store.load("legacy-v0")
    assert first == second
    assert path.read_bytes() == first_bytes
    assert first.schema_version == 3
    assert first.created_at == "2026-01-01T00:00:00Z"
    assert first.updated_at == "2026-01-02T00:00:00Z"
    assert first.title == "legacy title"
    assert len(first.message_ledger) == 1
    assert first.message_ledger[0].role == "user"
    assert first.message_ledger[0].created_at is None
    assert first.message_ledger[0].reply_to_message_id is None


def test_v1_missing_metadata_fields_is_treated_as_v0(tmp_path):
    store = _store(tmp_path)
    path = store._path("partial-v1")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "partial-v1",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "messages": [],
            }
        ),
        encoding="utf-8",
    )
    migrated = store.load("partial-v1")
    assert migrated.title == "（空会话）"
    assert migrated.metadata_version == 1


@pytest.mark.parametrize("version", [True, "1", -1, 4])
def test_invalid_or_future_schema_version_fails_closed(tmp_path, version):
    store = _store(tmp_path)
    path = store._path("unsupported")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": version, "id": "unsupported"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        store.load("unsupported")


def test_migration_replace_failure_preserves_original_v0(tmp_path, monkeypatch):
    store = _store(tmp_path)
    path = store._path("migration-failure")
    path.parent.mkdir(parents=True)
    original = json.dumps(
        {
            "schema_version": 0,
            "id": "migration-failure",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "messages": [],
        }
    ).encode()
    path.write_bytes(original)

    def fail_replace(_source, target):
        if target == path:
            raise OSError("migration replace failed")
        raise AssertionError(f"unexpected replace target: {target}")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="migration replace failed"):
        store.load("migration-failure")
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_update_save_requires_existing_session_and_cannot_recreate_deleted(tmp_path):
    store = _store(tmp_path)
    session = store.new_session()
    with pytest.raises(FileNotFoundError):
        store.save(session, [])
    store.save(session, [], must_exist=False)
    stale = store.load(session.id)
    assert store.delete(session.id)
    with pytest.raises(FileNotFoundError):
        store.save(stale, [{"role": "user", "content": "must not revive"}])
    assert not store._path(session.id).exists()
