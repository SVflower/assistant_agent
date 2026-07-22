"""Session CRUD 与隔离 Runtime 创建用例。

这里描述调用方“想完成什么”，例如创建、载入、检索或删除 Session。文件路径、LiteLLM 和终端
展示属于 adapter，不应渗入这些用例。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import unicodedata
from datetime import datetime
from typing import Literal, cast

from pydantic import ValidationError

from assistant_agent.application.models import (
    RunMeta,
    RunResumeInfo,
    Session,
    SessionMeta,
    is_public_run_status,
)
from assistant_agent.application.ports import (
    RunCatalogRepository,
    RuntimeFactoryPort,
    SessionExecutionLeaseManager,
    SessionRepository,
)
from assistant_agent.application.runs import SessionRuntime, inspect_run
from assistant_agent.contracts.capabilities import RuntimeCapabilities
from assistant_agent.contracts.charts import AnyChartArtifact
from assistant_agent.contracts.errors import (
    ArtifactNotFoundError,
    ArtifactUnavailableError,
    InvalidSessionCursorError,
    InvalidSessionLimitError,
    InvalidSessionMetadataError,
    InvalidSessionQueryError,
    RuntimeClosedError,
    SessionNotFoundError,
    SessionRunConflictError,
    SessionUnavailableError,
)
from assistant_agent.contracts.interactions import InteractionPort
from assistant_agent.contracts.sessions import (
    LastRunSummary,
    SessionCatalogPage,
    SessionSummary,
    UpdateSessionMetadataRequest,
)
from assistant_agent.contracts.time import normalize_utc_timestamp, parse_utc_timestamp

_CURSOR_VERSION = 1
_CURSOR_DOMAIN = b"assistant-agent:m23-r1:session-cursor:"
_CURSOR_KEY = secrets.token_bytes(32)


class _LastRunUnavailableError(Exception):
    pass


def _normalize_query(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _as_utc_iso(value: str) -> str:
    return normalize_utc_timestamp(value)


def _cursor_signature(data: bytes) -> str:
    return hmac.new(_CURSOR_KEY, _CURSOR_DOMAIN + data, hashlib.sha256).hexdigest()


def _encode_cursor(query: str, item: SessionMeta) -> str:
    payload = {
        "id": item.id,
        "query": query,
        "updated_at": _as_utc_iso(item.updated_at),
        "version": _CURSOR_VERSION,
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    envelope = json.dumps(
        {"data": payload, "signature": _cursor_signature(data)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(envelope).decode().rstrip("=")


def _decode_cursor(cursor: str, query: str) -> tuple[datetime, str]:
    try:
        if not cursor or not isinstance(cursor, str):
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        envelope = json.loads(base64.b64decode(cursor + padding, altchars=b"-_", validate=True))
        if not isinstance(envelope, dict) or set(envelope) != {"data", "signature"}:
            raise ValueError
        payload = envelope["data"]
        if not isinstance(payload, dict) or set(payload) != {
            "id",
            "query",
            "updated_at",
            "version",
        }:
            raise ValueError
        data = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if not isinstance(envelope["signature"], str) or not hmac.compare_digest(
            envelope["signature"], _cursor_signature(data)
        ):
            raise ValueError
        if payload["version"] != _CURSOR_VERSION or payload["query"] != query:
            raise ValueError
        if not isinstance(payload["updated_at"], str) or not isinstance(payload["id"], str):
            raise ValueError
        return parse_utc_timestamp(payload["updated_at"]), payload["id"]
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidSessionCursorError("Session cursor 无效") from exc


class AgentService:
    """只依赖 RuntimeFactory 和 repository ports 的 Session 用例。

    每次 ``create_session``/``load_session`` 都获得隔离 Runtime。构造过程中如果 Session 操作失败，
    必须立刻关闭 Runtime，不能把半初始化的 MCP、WebClient 或进程监管线程留在服务进程中。
    """

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactoryPort,
        session_store: SessionRepository,
        run_store: RunCatalogRepository,
        session_leases: SessionExecutionLeaseManager,
        max_completed_runs: int,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._session_store = session_store
        self._run_store = run_store
        self._session_leases = session_leases
        self._max_completed_runs = max_completed_runs

    def create_session(
        self,
        *,
        interaction: InteractionPort | None = None,
        interactive: bool = True,
    ) -> SessionRuntime:
        runtime = self._runtime_factory(interaction, interactive, None)
        try:
            session = runtime.session_store.new_session(
                provider=runtime.config.active,
                model=runtime.config.active_provider.model,
            )
            runtime.session_store.save(session, [], must_exist=False)
            runtime.logger.bind_session(session.id)
            return SessionRuntime(runtime, session)
        except BaseException:
            runtime.close("session_create_failed")
            raise

    def load_session(
        self,
        session_id: str,
        *,
        interaction: InteractionPort | None = None,
        interactive: bool = True,
    ) -> SessionRuntime:
        runtime = self._runtime_factory(interaction, interactive, session_id)
        try:
            session = runtime.session_store.load(session_id)
            return SessionRuntime(runtime, session)
        except FileNotFoundError as exc:
            runtime.close("session_load_failed")
            raise SessionNotFoundError("Session 不存在") from exc
        except BaseException:
            runtime.close("session_load_failed")
            raise

    def list_sessions(self) -> list[SessionMeta]:
        return self._session_store.list()

    def get_session_summary(self, session_id: str) -> SessionSummary:
        """在线性化的 Session 锁内快照上构造公开 summary。"""

        def build_summary(session: Session) -> SessionSummary:
            try:
                last_run = self._run_store.last_for_session_locked(session_id)
                last_run_summary = self._last_run_summary(last_run)
            except (OSError, ValueError) as exc:
                raise _LastRunUnavailableError from exc
            return self._summary(self._session_meta(session), last_run_summary)

        try:
            return self._session_store.read_locked(session_id, build_summary)
        except _LastRunUnavailableError as exc:
            raise SessionUnavailableError("Session summary 暂不可用") from exc
        except FileNotFoundError as exc:
            raise SessionNotFoundError("Session 不存在") from exc
        except (OSError, ValueError) as exc:
            raise SessionUnavailableError("Session summary 暂不可用") from exc

    def catalog_sessions(
        self,
        query: str | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> SessionCatalogPage:
        if query is not None and not isinstance(query, str):
            raise InvalidSessionQueryError("Session query 必须是字符串")
        raw_query = query or ""
        if len(raw_query) > 200:
            raise InvalidSessionQueryError("Session query 不能超过 200 个字符")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidSessionLimitError("Session limit 必须在 1..100")
        normalized_query = _normalize_query(raw_query)
        cursor_key = _decode_cursor(cursor, normalized_query) if cursor is not None else None
        try:
            sessions = self._session_store.list()
            runs = self._run_store.list()
        except (OSError, ValueError) as exc:
            raise SessionUnavailableError("Session catalog 暂不可用") from exc
        last_runs: dict[str, RunMeta] = {}
        for run in runs:
            if run.session_id is None or not is_public_run_status(run.status):
                continue
            try:
                current = last_runs.get(run.session_id)
                if current is None or self._run_key(run) > self._run_key(current):
                    last_runs[run.session_id] = run
            except ValueError as exc:
                raise SessionUnavailableError("Session catalog 暂不可用") from exc
        filtered = [
            item
            for item in sessions
            if not normalized_query
            or normalized_query in _normalize_query(item.title)
            or normalized_query in _normalize_query(item.preview)
        ]
        try:
            filtered.sort(key=self._session_key, reverse=True)
            if cursor_key is not None:
                filtered = [item for item in filtered if self._session_key(item) < cursor_key]
        except ValueError as exc:
            raise SessionUnavailableError("Session catalog 暂不可用") from exc
        selected = filtered[: limit + 1]
        page_items = selected[:limit]
        try:
            summaries = tuple(
                self._summary(item, self._last_run_summary(last_runs.get(item.id)))
                for item in page_items
            )
        except ValueError as exc:
            raise SessionUnavailableError("Session catalog 暂不可用") from exc
        next_cursor = (
            _encode_cursor(normalized_query, page_items[-1]) if len(selected) > limit else None
        )
        return SessionCatalogPage(items=summaries, next_cursor=next_cursor)

    def update_session_metadata(
        self,
        session_id: str,
        title: str,
        expected_metadata_version: int,
    ) -> SessionSummary:
        try:
            request = UpdateSessionMetadataRequest.model_validate(
                {
                    "title": title,
                    "expected_metadata_version": expected_metadata_version,
                },
                strict=True,
            )
        except ValidationError as exc:
            raise InvalidSessionMetadataError("Session 元数据不合法") from exc
        try:
            runs = self._run_store.list()
            last_run = max(
                (
                    item
                    for item in runs
                    if item.session_id == session_id and is_public_run_status(item.status)
                ),
                key=self._run_key,
                default=None,
            )
            last_run_summary = self._last_run_summary(last_run)
        except (OSError, ValueError) as exc:
            raise SessionUnavailableError("Session 元数据暂不可用") from exc
        try:
            session = self._session_store.update_metadata(
                session_id,
                request.title,
                request.expected_metadata_version,
            )
        except FileNotFoundError as exc:
            raise SessionNotFoundError("Session 不存在") from exc
        except (OSError, ValueError) as exc:
            raise SessionUnavailableError("Session 元数据暂不可用") from exc
        return self._summary(self._session_meta(session), last_run_summary)

    @staticmethod
    def _last_run_summary(run: RunMeta | None) -> LastRunSummary | None:
        if run is not None and is_public_run_status(run.status):
            return LastRunSummary(
                id=run.id,
                status=run.status,
                updated_at=_as_utc_iso(run.updated_at),
            )
        return None

    @staticmethod
    def _run_key(run: RunMeta) -> tuple[datetime, str]:
        return parse_utc_timestamp(run.updated_at), run.id

    @staticmethod
    def _session_key(session: SessionMeta) -> tuple[datetime, str]:
        return parse_utc_timestamp(session.updated_at), session.id

    @staticmethod
    def _session_meta(session: Session) -> SessionMeta:
        return SessionMeta(
            id=session.id,
            title=session.title,
            title_source=session.title_source,
            metadata_version=session.metadata_version,
            created_at=session.created_at,
            updated_at=session.updated_at,
            message_count=len(session.message_ledger),
            preview=session.preview,
        )

    @staticmethod
    def _summary(meta: SessionMeta, last_run: LastRunSummary | None) -> SessionSummary:
        return SessionSummary(
            id=meta.id,
            title=meta.title,
            title_source=cast(Literal["auto", "user"], meta.title_source),
            metadata_version=meta.metadata_version,
            created_at=_as_utc_iso(meta.created_at),
            updated_at=_as_utc_iso(meta.updated_at),
            message_count=meta.message_count,
            preview=meta.preview,
            last_run=last_run,
        )

    def list_runs(self, *, session_id: str | None = None) -> list[RunMeta]:
        runs = self._run_store.list()
        return (
            runs if session_id is None else [item for item in runs if item.session_id == session_id]
        )

    def _inspect_run(self, run_id: str) -> RunResumeInfo:
        return inspect_run(self._run_store, run_id)

    def _delete_run(self, run_id: str, *, force: bool = False) -> bool:
        meta = next((item for item in self._run_store.list() if item.id == run_id), None)
        if meta is None:
            return False
        if meta.status in {"running", "paused"} and not force:
            raise SessionRunConflictError(f"Run 尚未结束：{run_id}")
        return self._run_store.delete(run_id)

    def prune_completed_runs(self) -> list[str]:
        return self._run_store.prune(self._max_completed_runs)

    def delete_session(self, session_id: str, *, force: bool = False) -> bool:
        lease = None
        if not force:
            from assistant_agent.contracts.errors import RunStillActiveError

            try:
                lease = self._session_leases.acquire(session_id)
            except RunStillActiveError as exc:
                raise SessionRunConflictError("Session 当前存在活跃 Run") from exc
        try:
            if not force:
                unfinished = [
                    item
                    for item in self._run_store.list()
                    if item.session_id == session_id and item.status in {"running", "paused"}
                ]
                if unfinished:
                    raise SessionRunConflictError(
                        f"Session 存在未完成 Run：{', '.join(item.id for item in unfinished)}"
                    )
            deleted = self._session_store.delete(session_id)
            if deleted:
                self._run_store.delete_session_runs(session_id)
            return deleted
        finally:
            if lease is not None:
                lease.release()

    def get_artifact(self, session_id: str, artifact_id: str) -> AnyChartArtifact:
        """按 Session 隔离读取完整 Artifact，不暴露持久化路径。"""
        try:
            session = self._session_store.load(session_id)
            for artifact in session.presentations:
                if artifact.artifact_id == artifact_id:
                    return artifact
            for meta in self._run_store.list():
                if meta.session_id != session_id:
                    continue
                document = self._run_store.load(meta.id).document
                from assistant_agent.agent.run.state import RunState, migrate_run_document

                state = RunState.model_validate(migrate_run_document(document))
                for artifact in state.presentations:
                    if artifact.artifact_id == artifact_id:
                        return artifact
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("图表 Artifact 不存在") from exc
        except ArtifactNotFoundError:
            raise
        except Exception as exc:
            raise ArtifactUnavailableError("图表 Artifact 暂不可用") from exc
        raise ArtifactNotFoundError("图表 Artifact 不存在")

    def probe_capabilities(self) -> RuntimeCapabilities:
        runtime = self._runtime_factory(None, False, None)
        try:
            if runtime.capabilities is None:
                raise RuntimeClosedError("Runtime 能力快照不可用")
            return runtime.capabilities
        finally:
            runtime.close("capability_probe_completed")
