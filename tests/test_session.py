"""会话持久化（SessionStore）测试。"""

from __future__ import annotations

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
    store.save(s, msgs)

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
    store.save(s, [{"role": "assistant", "content": "坏字符\udce8结尾"}])
    # 不崩即通过；文件应已写出
    assert store._path(s.id).exists()


def test_list_sorted_recent_first(tmp_path):
    store = _store(tmp_path)
    s1 = store.new_session()
    s1.created_at = "2026-07-01T10:00:00"
    store.save(s1, [{"role": "user", "content": "第一个"}])
    s2 = store.new_session()
    store.save(s2, [{"role": "user", "content": "第二个"}])

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
    )
    meta = store.list()[0]
    assert "帮我写个函数" in meta.preview


def test_delete(tmp_path):
    store = _store(tmp_path)
    s = store.new_session()
    store.save(s, [{"role": "user", "content": "x"}])
    assert store.delete(s.id) is True
    assert store.delete(s.id) is False  # 再删返回 False
    assert store.list() == []


def test_list_skips_corrupt_files(tmp_path):
    store = _store(tmp_path)
    s = store.new_session()
    store.save(s, [{"role": "user", "content": "ok"}])
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
    store.save(session, [{"role": "user", "content": "old"}])

    def fail_replace(_source, _target):
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        store.save(session, [{"role": "user", "content": "new"}])

    assert store.load(session.id).messages[0]["content"] == "old"
    assert not list((tmp_path / "sessions").glob("*.tmp"))
