"""本地 Managed Output Store。"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from assistant_agent.config.schema import OutputConfig
from assistant_agent.contracts.outputs import (
    OutputArtifactV1,
    OutputConflictError,
    OutputInvalidError,
    OutputLimitExceededError,
    OutputNotFoundError,
    OutputPayload,
    OutputUnavailableError,
)
from assistant_agent.contracts.time import utc_now_rfc3339

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PORTABLE_INVALID = re.compile(r'[<>:"/\\|?*]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class OutputStore:
    """输出文件唯一存储 owner；公共查询只返回不透明引用。"""

    def __init__(self, workspace_root: Path, config: OutputConfig) -> None:
        self.workspace_root = workspace_root.resolve()
        configured = Path(config.root).expanduser()
        self.root = (
            configured if configured.is_absolute() else self.workspace_root / configured
        ).resolve()
        self.config = config

    def publish_text(
        self,
        *,
        session_id: str,
        run_id: str,
        call_id: str,
        filename: str,
        media_type: str,
        content: str,
        disposition: str = "download",
        message_id: str | None = None,
        title: str | None = None,
    ) -> OutputArtifactV1:
        self._validate_identity(session_id, run_id, call_id)
        clean_name = self._validate_filename(filename)
        if media_type not in self.config.allowed_media_types:
            raise OutputInvalidError("不支持的输出 media_type")
        payload = content.encode("utf-8")
        if len(payload) > self.config.max_file_bytes:
            raise OutputLimitExceededError("输出超过单文件字节上限")
        existing = self.list(session_id, run_id=run_id)
        output_id = self._output_id(run_id, call_id)
        prior = next((item for item in existing if item.output_id == output_id), None)
        digest = hashlib.sha256(payload).hexdigest()
        if prior is not None:
            if (
                prior.content_hash == digest
                and prior.filename == clean_name
                and prior.media_type == media_type
                and prior.disposition == disposition
                and prior.title == title
            ):
                return prior
            raise OutputConflictError("相同 Run/call 已发布不同输出")
        session_items = self.list(session_id)
        if len(session_items) >= self.config.max_session_files:
            raise OutputLimitExceededError("Session 输出文件数已达上限")
        if len(existing) >= self.config.max_run_files:
            raise OutputLimitExceededError("Run 输出文件数已达上限")
        if sum(item.size_bytes for item in existing) + len(payload) > self.config.max_run_bytes:
            raise OutputLimitExceededError("Run 输出总字节已达上限")
        if (
            sum(item.size_bytes for item in session_items) + len(payload)
            > self.config.max_session_bytes
        ):
            raise OutputLimitExceededError("Session 输出总字节已达上限")

        now = datetime.now(UTC)
        directory = self._session_directory(session_id, now)
        directory.mkdir(parents=True, exist_ok=True)
        artifact = OutputArtifactV1(
            output_id=output_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            call_id=call_id,
            filename=clean_name,
            title=title,
            media_type=media_type,
            size_bytes=len(payload),
            content_hash=digest,
            created_at=utc_now_rfc3339(),
            disposition=cast(Literal["inline", "download"], disposition),
            preview_supported=media_type in self.config.preview_media_types,
        )
        payload_path = directory / f"{output_id}--{clean_name}"
        self._atomic_write(payload_path, payload)
        try:
            self._atomic_write(
                directory / f"{output_id}.json",
                json.dumps(
                    artifact.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
                ).encode("utf-8"),
            )
        except Exception:
            payload_path.unlink(missing_ok=True)
            raise
        return artifact

    def begin_text_draft(
        self,
        *,
        session_id: str,
        run_id: str,
        call_id: str,
        filename: str,
        media_type: str,
        disposition: str = "download",
        message_id: str | None = None,
        title: str | None = None,
    ) -> str:
        self._validate_identity(session_id, run_id, call_id)
        clean_name = self._validate_filename(filename)
        if media_type not in self.config.allowed_media_types:
            raise OutputInvalidError("不支持的输出 media_type")
        if disposition not in {"inline", "download"}:
            raise OutputInvalidError("输出 disposition 无效")
        draft_id = (
            "draft_"
            + hashlib.sha256(f"{session_id}\0{run_id}\0{call_id}".encode()).hexdigest()[:32]
        )
        directory = self._draft_directory(session_id, run_id, draft_id)
        run_drafts = self.root / ".drafts" / session_id / run_id
        draft_count = sum(1 for _ in run_drafts.glob("draft_*/draft.json"))
        if not directory.exists() and draft_count >= self.config.max_run_files:
            raise OutputLimitExceededError("Run 输出草稿数量已达上限")
        metadata = {
            "draft_id": draft_id,
            "session_id": session_id,
            "run_id": run_id,
            "call_id": call_id,
            "filename": clean_name,
            "media_type": media_type,
            "disposition": disposition,
            "message_id": message_id,
            "title": title,
        }
        payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
        metadata_path = directory / "draft.json"
        if metadata_path.exists():
            try:
                if metadata_path.read_bytes() == payload:
                    return draft_id
            except OSError as exc:
                raise OutputUnavailableError("输出草稿暂不可读") from exc
            raise OutputConflictError("相同调用已创建不同输出草稿")
        directory.mkdir(parents=True, exist_ok=True)
        self._atomic_write(metadata_path, payload)
        return draft_id

    def append_text_draft(
        self,
        *,
        session_id: str,
        run_id: str,
        draft_id: str,
        chunk_index: int,
        content: str,
    ) -> int:
        _metadata, directory = self._load_draft(session_id, run_id, draft_id)
        if (directory / "finalized.json").exists():
            raise OutputConflictError("输出草稿已经完成")
        if chunk_index < 0 or chunk_index >= self.config.max_draft_chunks:
            raise OutputLimitExceededError("输出草稿分块数量超过上限")
        chunk = content.encode("utf-8")
        if not chunk:
            raise OutputInvalidError("输出草稿分块不能为空")
        if len(chunk) > self.config.max_chunk_bytes:
            raise OutputLimitExceededError(
                f"单个输出分块超过 {self.config.max_chunk_bytes} UTF-8 bytes"
            )
        chunks = self._draft_chunks(directory)
        expected = len(chunks)
        path = directory / f"chunk-{chunk_index:04d}.txt"
        if chunk_index < expected:
            try:
                if path.read_bytes() == chunk:
                    return sum(item.stat().st_size for item in chunks)
            except OSError as exc:
                raise OutputUnavailableError("输出草稿分块暂不可读") from exc
            raise OutputConflictError("相同分块序号已写入不同内容")
        if chunk_index != expected:
            raise OutputInvalidError(f"输出分块必须连续，下一块应为 {expected}")
        current_size = sum(item.stat().st_size for item in chunks)
        if current_size + len(chunk) > self.config.max_file_bytes:
            raise OutputLimitExceededError("输出草稿超过单文件字节上限")
        run_draft_root = self.root / ".drafts" / session_id / run_id
        run_draft_bytes = sum(
            path.stat().st_size for path in run_draft_root.glob("draft_*/chunk-*.txt")
        )
        if run_draft_bytes + len(chunk) > self.config.max_run_bytes:
            raise OutputLimitExceededError("Run 输出草稿总字节已达上限")
        session_draft_root = self.root / ".drafts" / session_id
        session_draft_bytes = sum(
            path.stat().st_size for path in session_draft_root.glob("**/chunk-*.txt")
        )
        if session_draft_bytes + len(chunk) > self.config.max_session_bytes:
            raise OutputLimitExceededError("Session 输出草稿总字节已达上限")
        self._atomic_write(path, chunk)
        return current_size + len(chunk)

    def finalize_text_draft(
        self, *, session_id: str, run_id: str, draft_id: str
    ) -> OutputArtifactV1:
        metadata, directory = self._load_draft(session_id, run_id, draft_id)
        finalized_path = directory / "finalized.json"
        if finalized_path.exists():
            try:
                output_id = str(json.loads(finalized_path.read_text(encoding="utf-8"))["output_id"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise OutputUnavailableError("输出草稿完成标记损坏") from exc
            return self.get(session_id, output_id)
        chunks = self._draft_chunks(directory)
        if not chunks:
            raise OutputInvalidError("输出草稿没有内容")
        try:
            payload = b"".join(path.read_bytes() for path in chunks)
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OutputInvalidError("输出草稿不是有效 UTF-8") from exc
        except OSError as exc:
            raise OutputUnavailableError("输出草稿暂不可读") from exc
        artifact = self.publish_text(
            session_id=session_id,
            run_id=run_id,
            call_id=str(metadata["call_id"]),
            filename=str(metadata["filename"]),
            media_type=str(metadata["media_type"]),
            content=content,
            disposition=str(metadata["disposition"]),
            message_id=(str(metadata["message_id"]) if metadata.get("message_id") else None),
            title=str(metadata["title"]) if metadata.get("title") is not None else None,
        )
        self._atomic_write(
            finalized_path,
            json.dumps({"output_id": artifact.output_id}, sort_keys=True).encode("utf-8"),
        )
        return artifact

    def reset_text_draft(self, *, session_id: str, run_id: str, draft_id: str) -> None:
        """丢弃未发布分块，供暂停、崩溃后的捕获轮从头安全重放。"""
        _metadata, directory = self._load_draft(session_id, run_id, draft_id)
        if (directory / "finalized.json").exists():
            raise OutputConflictError("输出草稿已经完成")
        for path in self._draft_chunks(directory):
            path.unlink(missing_ok=True)

    def discard_run_drafts(self, session_id: str, run_id: str) -> None:
        self._validate_identity(session_id, run_id)
        directory = self.root / ".drafts" / session_id / run_id
        if self._within_root(directory):
            self._remove_tree(directory)

    def list(
        self, session_id: str, *, run_id: str | None = None
    ) -> builtins.list[OutputArtifactV1]:
        self._validate_identity(session_id)
        items: builtins.list[OutputArtifactV1] = []
        if not self.root.exists():
            return items
        for metadata in self.root.glob(f"**/{session_id}/out_*.json"):
            if not self._within_root(metadata):
                continue
            artifact = self._read_metadata(metadata)
            if artifact.session_id != session_id or (
                run_id is not None and artifact.run_id != run_id
            ):
                continue
            items.append(artifact)
        return sorted(items, key=lambda item: (item.created_at, item.output_id))

    def get(self, session_id: str, output_id: str) -> OutputArtifactV1:
        return self._find(session_id, output_id)[0]

    def get_payload(self, session_id: str, output_id: str) -> OutputPayload:
        artifact, metadata = self._find(session_id, output_id)
        data_path = self._payload_path(metadata, artifact)
        try:
            payload = data_path.read_bytes()
        except OSError as exc:
            raise OutputUnavailableError("输出载荷暂不可读") from exc
        if (
            len(payload) != artifact.size_bytes
            or hashlib.sha256(payload).hexdigest() != artifact.content_hash
        ):
            raise OutputUnavailableError("输出载荷完整性校验失败")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OutputUnavailableError("输出载荷不是有效 UTF-8") from exc
        return OutputPayload(artifact=artifact, content=content)

    def local_path(self, session_id: str, output_id: str) -> Path:
        artifact, metadata = self._find(session_id, output_id)
        return self._payload_path(metadata, artifact)

    def delete(self, session_id: str, output_id: str) -> None:
        _artifact, metadata = self._find(session_id, output_id)
        self._payload_path(metadata, _artifact).unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)

    def delete_session(self, session_id: str) -> None:
        for artifact in self.list(session_id):
            self.delete(session_id, artifact.output_id)
        for directory in sorted(self.root.glob(f"**/{session_id}"), reverse=True):
            if self._within_root(directory):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        drafts = self.root / ".drafts" / session_id
        if self._within_root(drafts):
            self._remove_tree(drafts)

    def fork(
        self,
        source: OutputArtifactV1,
        *,
        target_session_id: str,
        target_message_id: str,
    ) -> OutputArtifactV1:
        payload = self.get_payload(source.session_id, source.output_id)
        return self.publish_text(
            session_id=target_session_id,
            run_id=f"fork-{source.output_id}",
            call_id=source.output_id,
            filename=source.filename,
            title=source.title,
            media_type=source.media_type,
            content=payload.content,
            disposition=source.disposition,
            message_id=target_message_id,
        )

    def _session_directory(self, session_id: str, now: datetime) -> Path:
        if self.config.layout == "flat":
            path = self.root / session_id
        elif self.config.layout == "date":
            path = self.root / f"{now:%Y-%m-%d}" / session_id
        else:
            path = self.root / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}" / session_id
        resolved = path.resolve()
        if not self._within_root(resolved):
            raise OutputInvalidError("输出路径越界")
        return resolved

    def _draft_directory(self, session_id: str, run_id: str, draft_id: str) -> Path:
        self._validate_identity(session_id, run_id, draft_id)
        path = (self.root / ".drafts" / session_id / run_id / draft_id).resolve()
        if not self._within_root(path):
            raise OutputInvalidError("输出草稿路径越界")
        return path

    def _load_draft(
        self, session_id: str, run_id: str, draft_id: str
    ) -> tuple[dict[str, object], Path]:
        directory = self._draft_directory(session_id, run_id, draft_id)
        try:
            value = json.loads((directory / "draft.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise OutputNotFoundError("输出草稿不存在") from exc
        except (OSError, ValueError) as exc:
            raise OutputUnavailableError("输出草稿元数据损坏") from exc
        if not isinstance(value, dict) or any(
            value.get(key) != expected
            for key, expected in (
                ("draft_id", draft_id),
                ("session_id", session_id),
                ("run_id", run_id),
            )
        ):
            raise OutputUnavailableError("输出草稿归属无效")
        return value, directory

    @staticmethod
    def _draft_chunks(directory: Path) -> builtins.list[Path]:
        chunks = sorted(directory.glob("chunk-*.txt"))
        expected = [directory / f"chunk-{index:04d}.txt" for index in range(len(chunks))]
        if chunks != expected:
            raise OutputUnavailableError("输出草稿分块序列损坏")
        return chunks

    @staticmethod
    def _remove_tree(directory: Path) -> None:
        if not directory.exists():
            return
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
            else:
                try:
                    path.rmdir()
                except OSError:
                    pass
        try:
            directory.rmdir()
        except OSError:
            pass

    def _find(self, session_id: str, output_id: str) -> tuple[OutputArtifactV1, Path]:
        self._validate_identity(session_id, output_id)
        for metadata in self.root.glob(f"**/{session_id}/{output_id}.json"):
            if self._within_root(metadata):
                artifact = self._read_metadata(metadata)
                if artifact.session_id == session_id and artifact.output_id == output_id:
                    return artifact, metadata
        raise OutputNotFoundError("输出不存在")

    def _read_metadata(self, path: Path) -> OutputArtifactV1:
        try:
            return OutputArtifactV1.model_validate_json(
                path.read_text(encoding="utf-8"), strict=True
            )
        except Exception as exc:
            raise OutputUnavailableError("输出元数据损坏") from exc

    def _payload_path(self, metadata: Path, artifact: OutputArtifactV1) -> Path:
        path = metadata.parent / f"{artifact.output_id}--{artifact.filename}"
        if not self._within_root(path):
            raise OutputUnavailableError("输出载荷路径无效")
        return path

    def _within_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _validate_identity(*values: str) -> None:
        if any(not value or not _SAFE_ID.fullmatch(value) for value in values):
            raise OutputInvalidError("输出归属标识无效")

    @staticmethod
    def _validate_filename(filename: str) -> str:
        if filename != Path(filename).name or filename in {".", ".."}:
            raise OutputInvalidError("filename 必须是 basename")
        stem = filename.split(".", 1)[0].upper()
        if (
            not filename
            or len(filename) > 180
            or _CONTROL.search(filename)
            or _PORTABLE_INVALID.search(filename)
            or filename.endswith((" ", "."))
            or stem in _WINDOWS_RESERVED
        ):
            raise OutputInvalidError("filename 无效")
        return filename

    @staticmethod
    def _output_id(run_id: str, call_id: str) -> str:
        return "out_" + hashlib.sha256(f"{run_id}\0{call_id}".encode()).hexdigest()[:32]

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
