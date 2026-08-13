"""版本化 Session JSON 存储、迁移与跨进程文档锁。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, TypeVar, cast

from assistant_agent.application.models import (
    EMPTY_SESSION_TITLE,
    SESSION_SCHEMA_VERSION,
    Session,
    SessionMeta,
    automatic_session_title,
)
from assistant_agent.application.ports import AttachmentRepository, OutputRepository
from assistant_agent.contracts.attachments import (
    MessageContentV1,
    UserMessageInputV1,
    parse_message_content,
    remap_content_attachments,
)
from assistant_agent.contracts.errors import (
    IdempotencyConflictError,
    SessionMigrationRequiredError,
    UnsupportedSessionSchemaError,
)
from assistant_agent.contracts.sessions import PublicMessageSnapshot
from assistant_agent.contracts.time import (
    normalize_utc_timestamp,
    parse_utc_timestamp,
    utc_now_rfc3339,
)
from assistant_agent.persistence.session_fork import build_forked_session
from assistant_agent.persistence.session_lifecycle import SessionLifecycle

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MESSAGE_ID = re.compile(r"^msg_[a-f0-9]{24}$")
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_T = TypeVar("_T")


def _now_iso() -> str:
    return utc_now_rfc3339()


def new_session_id() -> str:
    """时间戳 + 短随机后缀：可读、可排序、不撞。"""
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def _valid_title(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 100
        and any(not char.isspace() for char in value)
    )


def _stable_message_id(session_id: str, ordinal: int, role: str) -> str:
    payload = f"session-message:{session_id}:{ordinal}:{role}".encode()
    return "msg_" + hashlib.sha256(payload).hexdigest()[:24]


def _public_raw_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        raise SessionMigrationRequiredError("Session messages 不是合法数组")
    public: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user" or (role == "assistant" and not message.get("tool_calls")):
            public.append(message)
    return public


def _proven_message_time(message: dict[str, Any]) -> str | None:
    value = message.get("created_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        return normalize_utc_timestamp(value)
    except ValueError:
        return None


def _validate_document(data: dict[str, Any]) -> dict[str, Any]:
    """只验证当前 Session schema；读取绝不重写持久化文档。"""
    version = data.get("schema_version")
    if version != SESSION_SCHEMA_VERSION:
        raise UnsupportedSessionSchemaError(
            f"Session schema 不兼容：需要 v{SESSION_SCHEMA_VERSION}",
            expected_version=SESSION_SCHEMA_VERSION,
            actual_version=version,
        )
    if not _valid_title(data.get("title")):
        raise ValueError("Session title 不合法")
    if data.get("title_source") not in {"auto", "user"}:
        raise ValueError("Session title_source 不合法")
    metadata_version = data.get("metadata_version")
    if type(metadata_version) is not int or metadata_version < 1:
        raise ValueError("Session metadata_version 不合法")
    if "message_ledger" not in data:
        raise SessionMigrationRequiredError("Session v3 缺少 message_ledger")
    normalize_utc_timestamp(str(data.get("created_at") or ""))
    normalize_utc_timestamp(str(data.get("updated_at") or ""))
    return data


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]


def _unlock_file(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:
        pass


class SessionStore:
    """会话文件的存取；每次文档读改写都持有短时跨进程锁。

    lifecycle 锁处理删除/创建世代，document 锁保护一份 Session 的读改写事务。只锁最终 write 不够：
    两个进程可能基于同一旧版本各自计算 ledger 或 fork，随后互相覆盖。
    """

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        lifecycle_dir: str | Path | None = None,
        attachment_store: AttachmentRepository | None = None,
        output_store: OutputRepository | None = None,
    ) -> None:
        if base_dir is None:
            from assistant_agent.config.paths import state_paths

            base_dir = state_paths().sessions
        self._dir = Path(base_dir)
        self._lifecycle = SessionLifecycle(lifecycle_dir or self._dir.parent / "session-lifecycle")
        self._attachments = attachment_store
        self._outputs = output_store

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("非法会话 ID")
        root = self._dir.resolve()
        path = (root / f"{session_id}.json").resolve()
        if path.parent != root:
            raise ValueError("会话路径超出存储目录")
        return path

    def _lock_path(self, session_id: str) -> Path:
        return self._path(session_id).with_suffix(".lock")

    @contextmanager
    def _document_lock(self, session_id: str) -> Iterator[None]:
        # 线程锁解决同一进程竞争，文件锁解决多个 API worker 竞争。两层都需要；
        # Windows/POSIX 的文件锁行为不同，由上方 `_lock_file` 统一适配。
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_path(session_id)
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(path, threading.Lock())
        with thread_lock:
            handle = path.open("a+b")
            try:
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                _lock_file(handle)
                yield
            finally:
                _unlock_file(handle)
                handle.close()

    def new_session(self, provider: str = "", model: str = "") -> Session:
        now = _now_iso()
        return Session(
            id=new_session_id(),
            created_at=now,
            updated_at=now,
            provider=provider,
            model=model,
        )

    def _read_locked(self, session_id: str) -> Session:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"会话不存在：{session_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Session 文档根节点必须是 object")
        session = Session.from_dict(_validate_document(raw))
        if session.id != session_id:
            raise ValueError("会话文件 ID 与请求 ID 不一致")
        return session

    def _atomic_write_locked(self, session: Session) -> None:
        # JSON 先完整写入同目录临时文件，再原子替换正式文件。异常时只删除临时文件，
        # 原 Session 保持可读，调用方不会观察到一半新、一半旧的文档。
        if session.schema_version != SESSION_SCHEMA_VERSION:
            raise UnsupportedSessionSchemaError(
                f"Session schema 不兼容：需要 v{SESSION_SCHEMA_VERSION}",
                expected_version=SESSION_SCHEMA_VERSION,
                actual_version=session.schema_version,
            )
        text = json.dumps(session.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
        payload = text.encode("utf-8", errors="replace")
        target = self._path(session.id)
        fd, temp_name = tempfile.mkstemp(prefix=f".{session.id}-", suffix=".tmp", dir=self._dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            self._fsync_directory()
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def save(
        self,
        session: Session,
        messages: list[dict[str, Any]] | None = None,
        *,
        must_exist: bool = True,
    ) -> None:
        """锁内 fresh load，并把内容变化合并到最新元数据后原子替换。"""
        with self._lifecycle.lock(session.id):
            if self._lifecycle.is_deleted_locked(session.id):
                raise FileNotFoundError(f"会话已删除：{session.id}")
            with self._document_lock(session.id):
                try:
                    fresh = self._read_locked(session.id)
                except FileNotFoundError:
                    fresh = None
                if must_exist and fresh is None:
                    raise FileNotFoundError(f"会话不存在：{session.id}")
                if not must_exist and fresh is not None:
                    raise FileExistsError(f"会话已存在：{session.id}")
                if messages is not None:
                    session.messages = messages
                pending_ledger = list(session.message_ledger)
                if fresh is not None:
                    session.title = fresh.title
                    session.title_source = fresh.title_source
                    session.metadata_version = fresh.metadata_version
                    session.schema_version = fresh.schema_version
                    session.created_at = fresh.created_at
                    session.message_ledger = fresh.message_ledger
                    session.fork_origin = fresh.fork_origin
                    if len(pending_ledger) > len(session.message_ledger):
                        if pending_ledger[: len(session.message_ledger)] != session.message_ledger:
                            raise SessionMigrationRequiredError("待保存 ledger 与最新 Session 冲突")
                        session.message_ledger = pending_ledger
                self._synchronize_ledger(session)
                generated = automatic_session_title(session.messages)
                if (
                    session.title_source == "auto"
                    and session.title == EMPTY_SESSION_TITLE
                    and generated != EMPTY_SESSION_TITLE
                ):
                    session.title = generated
                    session.metadata_version += 1
                session.updated_at = _now_iso()
                self._atomic_write_locked(session)

    def _synchronize_ledger(self, session: Session) -> None:
        # message_ledger 是公开会话的权威事实，messages 只是模型工作历史。只有当二者仍保持
        # 可证明的前缀关系时才能追加；compaction 改写 messages 后绝不能反推并覆盖 ledger。
        # ``SessionStore.save`` is an internal write convenience used by CLI tests and tools.
        # It canonicalizes a caller-provided plain user string before the v4 document is written;
        # loading an old on-disk schema remains fail-closed.
        for raw in session.messages:
            if raw.get("role") == "user" and isinstance(raw.get("content"), str):
                raw["content"] = UserMessageInputV1.from_text(raw["content"]).content.model_dump(
                    mode="json"
                )
            elif raw.get("role") == "user" and isinstance(raw.get("content"), MessageContentV1):
                raw["content"] = raw["content"].model_dump(mode="json")
        public = _public_raw_messages(session.messages)
        ledger = session.message_ledger
        prefix_matches = len(public) >= len(ledger) and all(
            raw.get("role") == saved.role
            and (
                parse_message_content(raw.get("content")).model_dump(mode="json")
                == cast(MessageContentV1, saved.content).model_dump(mode="json")
                if saved.role == "user"
                else str(raw.get("content") or "") == saved.content
            )
            for raw, saved in zip(public, ledger, strict=False)
        )
        if ledger and not prefix_matches:
            # Compaction may replace the model-facing history. The public ledger is authoritative
            # and must never be reconstructed from that lossy representation.
            return
        if len(public) == len(ledger):
            return
        current_user_id = next(
            (message.id for message in reversed(ledger) if message.role == "user"), None
        )
        used_ids = {message.id for message in ledger}
        for ordinal, raw in enumerate(public[len(ledger) :], start=len(ledger)):
            role = cast(
                Literal["user", "assistant"],
                "user" if raw.get("role") == "user" else "assistant",
            )
            message_id = _stable_message_id(session.id, ordinal, role)
            if message_id in used_ids:
                raise SessionMigrationRequiredError("Session message ID 冲突")
            if role == "user":
                current_user_id = message_id
                reply_to = None
            else:
                if current_user_id is None:
                    raise SessionMigrationRequiredError("assistant message 缺少可证明的 user 归属")
                reply_to = current_user_id
            ledger.append(
                PublicMessageSnapshot(
                    id=message_id,
                    role=role,
                    created_at=_proven_message_time(raw),
                    reply_to_message_id=reply_to,
                    content=(
                        parse_message_content(raw.get("content"))
                        if role == "user"
                        else str(raw.get("content") or "")
                    ),
                )
            )
            used_ids.add(message_id)

    def load(self, session_id: str) -> Session:
        """载入当前 v3 Session；其他版本明确拒绝。"""
        return self.read_locked(session_id, lambda session: session)

    def read_locked(self, session_id: str, reader: Callable[[Session], _T]) -> _T:
        """在 lifecycle/document 锁内读取当前 Session 并执行只读映射。"""
        with self._lifecycle.lock(session_id):
            if self._lifecycle.is_deleted_locked(session_id):
                raise FileNotFoundError(f"会话已删除：{session_id}")
            with self._document_lock(session_id):
                return reader(self._read_locked(session_id))

    def update_metadata(self, session_id: str, title: str, expected_version: int) -> Session:
        if not _valid_title(title):
            raise ValueError("Session title 不合法")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_metadata_version 不合法")
        with self._lifecycle.lock(session_id):
            if self._lifecycle.is_deleted_locked(session_id):
                raise FileNotFoundError(f"会话已删除：{session_id}")
            with self._document_lock(session_id):
                session = self._read_locked(session_id)
                if session.metadata_version != expected_version:
                    from assistant_agent.contracts.errors import SessionMetadataConflictError

                    raise SessionMetadataConflictError(
                        "Session 元数据版本冲突",
                        current_metadata_version=session.metadata_version,
                    )
                session.title = title
                session.title_source = "user"
                session.metadata_version += 1
                session.updated_at = _now_iso()
                self._atomic_write_locked(session)
                return session

    def fork_session(
        self,
        source_session_id: str,
        before_user_message_id: str,
        key_hash: str,
        request_hash: str,
    ) -> tuple[Session, bool]:
        """在源文档锁内读取一致快照并一次发布完整 fork 文档。"""
        with self._lifecycle.lock(source_session_id):
            if self._lifecycle.is_deleted_locked(source_session_id):
                raise FileNotFoundError(f"会话已删除：{source_session_id}")
            with self._document_lock(source_session_id):
                source = self._read_locked(source_session_id)
                replay = self._find_fork_locked(source_session_id, key_hash)
                if replay is not None:
                    origin = replay.fork_origin or {}
                    if (
                        origin.get("request_hash") != request_hash
                        or origin.get("before_user_message_id") != before_user_message_id
                    ):
                        raise IdempotencyConflictError("幂等键已用于其他 fork 请求")
                    return replay, False
                committed_at = _now_iso()
                while True:
                    target_id = new_session_id()
                    if not self._path(target_id).exists():
                        break
                try:
                    target = build_forked_session(
                        source,
                        before_user_message_id=before_user_message_id,
                        target_session_id=target_id,
                        committed_at=committed_at,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        output_store=self._outputs,
                    )
                    refs = tuple(
                        ref
                        for message in target.message_ledger
                        if message.role == "user"
                        for ref in cast(MessageContentV1, message.content).attachment_refs()
                    )
                    if refs:
                        if self._attachments is None:
                            raise SessionMigrationRequiredError("Attachment Store 未配置")
                        mapping = self._attachments.fork(source.id, target_id, refs)
                        target.message_ledger = [
                            message.model_copy(
                                update={
                                    "content": remap_content_attachments(message.content, mapping)
                                }
                            )
                            if message.role == "user"
                            else message
                            for message in target.message_ledger
                        ]
                        target.messages = [
                            {
                                "role": message.role,
                                "content": (
                                    cast(MessageContentV1, message.content).model_dump(mode="json")
                                    if message.role == "user"
                                    else message.content
                                ),
                            }
                            for message in target.message_ledger
                        ]
                    with self._lifecycle.lock(target_id):
                        with self._document_lock(target_id):
                            self._atomic_write_locked(target)
                except BaseException:
                    self._path(target_id).unlink(missing_ok=True)
                    if self._attachments is not None:
                        self._attachments.delete_session(target_id)
                    if self._outputs is not None:
                        self._outputs.delete_session(target_id)
                    raise
                return target, True

    def _find_fork_locked(self, source_session_id: str, key_hash: str) -> Session | None:
        if not self._dir.is_dir():
            return None
        for path in self._dir.glob("*.json"):
            if path.stem == source_session_id:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            origin = raw.get("fork_origin")
            if (
                not isinstance(origin, dict)
                or origin.get("source_session_id") != source_session_id
                or origin.get("key_hash") != key_hash
            ):
                continue
            try:
                return Session.from_dict(_validate_document(raw))
            except (ValueError, KeyError) as exc:
                raise SessionMigrationRequiredError("fork 幂等结果无法安全恢复") from exc
        return None

    def list(self) -> list[SessionMeta]:
        if not self._dir.is_dir():
            return []
        metas: list[SessionMeta] = []
        for path in self._dir.glob("*.json"):
            try:
                session = self.load(path.stem)
            except UnsupportedSessionSchemaError:
                raise
            except SessionMigrationRequiredError:
                # 无法安全迁移的旧文档保持原样，仍可按 ID 显式诊断；单个坏 Session
                # 不得拖垮整个 catalog。未知未来 schema 仍由上方专门分支 fail closed。
                continue
            except (json.JSONDecodeError, KeyError, OSError, UnicodeError, ValueError):
                continue
            metas.append(
                SessionMeta(
                    id=session.id,
                    title=session.title,
                    title_source=session.title_source,
                    metadata_version=session.metadata_version,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    message_count=len(session.message_ledger),
                    preview=session.preview,
                )
            )
        metas.sort(key=lambda item: (parse_utc_timestamp(item.updated_at), item.id), reverse=True)
        return metas

    def delete(self, session_id: str) -> bool:
        with self._lifecycle.lock(session_id):
            if self._lifecycle.is_deleted_locked(session_id):
                return False
            with self._document_lock(session_id):
                path = self._path(session_id)
                if not path.is_file():
                    return False
                self._lifecycle.mark_deleted_locked(session_id)
                path.unlink()
                return True

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(self._dir, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
