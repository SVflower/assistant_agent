"""Atomic, session-scoped Attachment Store.

Only this adapter handles filesystem paths, MIME validation and image normalization. Public
contracts and checkpoints contain immutable opaque refs, never paths or encoded payloads.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from assistant_agent.config.schema import AttachmentsConfig
from assistant_agent.contracts.attachments import (
    AttachmentPayloadV1,
    AttachmentRefV1,
    AttachmentSummaryV1,
    AttachmentUploadV1,
    TextFormat,
)
from assistant_agent.contracts.errors import (
    AttachmentInvalidError,
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    AttachmentUnavailableError,
)

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ATTACHMENT_ID = re.compile(r"^att_[a-f0-9]{24}$")
_TEXT_SUFFIXES: dict[str, TextFormat] = {
    ".txt": "plain",
    ".md": "markdown",
    ".csv": "csv",
    ".json": "json",
    ".log": "log",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
_TEXT_MEDIA: dict[str, TextFormat] = {
    "text/plain": "plain",
    "text/markdown": "markdown",
    "text/csv": "csv",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
    "application/yaml": "yaml",
    "text/yaml": "yaml",
    "application/x-yaml": "yaml",
}
_IMAGE_FORMATS = {
    "PNG": ("image/png", "PNG"),
    "JPEG": ("image/jpeg", "JPEG"),
    "WEBP": ("image/webp", "WEBP"),
}
_CONTROL_BYTES = frozenset(range(0, 9)) | frozenset({11, 12}) | frozenset(range(14, 32))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    name = Path(value).name.strip()
    if not name or name in {".", ".."}:
        raise AttachmentInvalidError("附件名称不合法")
    return name[:200]


def _too_large(message: str, resource: str, used: int, limit: int) -> AttachmentTooLargeError:
    return AttachmentTooLargeError(message, resource=resource, used=used, limit=limit)


class AttachmentStore:
    """Stores normalized payloads under one workspace-owned root."""

    def __init__(self, root: Path, config: AttachmentsConfig) -> None:
        self._root = Path(root)
        self._config = config
        self._lock = threading.RLock()

    def _session_dir(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id):
            raise AttachmentInvalidError("Attachment Session ID 不合法")
        return self._root / session_id

    def _item_dir(self, session_id: str, attachment_id: str) -> Path:
        if not _ATTACHMENT_ID.fullmatch(attachment_id):
            raise AttachmentInvalidError("Attachment ID 不合法")
        return self._session_dir(session_id) / attachment_id

    def ingest(
        self, session_id: str, uploads: Sequence[AttachmentUploadV1]
    ) -> tuple[AttachmentSummaryV1, ...]:
        if not uploads:
            return ()
        if len(uploads) > self._config.max_attachments_per_message:
            raise _too_large(
                "单消息附件数量超过限制",
                "attachment_count",
                len(uploads),
                self._config.max_attachments_per_message,
            )
        if sum(len(item.data) for item in uploads) > self._config.max_total_bytes_per_run:
            raise _too_large(
                "单次上传附件总量超过限制",
                "total_bytes",
                sum(len(item.data) for item in uploads),
                self._config.max_total_bytes_per_run,
            )
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".ingest-", dir=self._root))
            published: list[Path] = []
            summaries: list[AttachmentSummaryV1] = []
            try:
                image_count = 0
                for upload in uploads:
                    ref, normalized = self._normalize(session_id, upload)
                    image_count += ref.kind == "image"
                    if image_count > self._config.max_images_per_message:
                        raise _too_large(
                            "单消息图片数量超过限制",
                            "image_count",
                            image_count,
                            self._config.max_images_per_message,
                        )
                    item_stage = stage / ref.attachment_id
                    item_stage.mkdir()
                    (item_stage / "payload.bin").write_bytes(normalized)
                    self._write_meta(item_stage, ref, bound=False)
                    summaries.append(
                        AttachmentSummaryV1(
                            attachment=ref,
                            approximate_tokens=(
                                max(1, (ref.char_count or 0) // 4) if ref.kind == "text" else None
                            ),
                        )
                    )
                target_parent = self._session_dir(session_id)
                target_parent.mkdir(parents=True, exist_ok=True)
                for item_stage in stage.iterdir():
                    target = target_parent / item_stage.name
                    os.replace(item_stage, target)
                    published.append(target)
                return tuple(summaries)
            except BaseException:
                for path in published:
                    shutil.rmtree(path, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    def _normalize(
        self, session_id: str, upload: AttachmentUploadV1
    ) -> tuple[AttachmentRefV1, bytes]:
        name = _safe_name(upload.display_name)
        data = bytes(upload.data)
        if not data:
            raise AttachmentInvalidError("附件不能为空")
        image = self._normalize_image(data)
        if image is not None:
            normalized, media_type, width, height = image
            if upload.media_type and upload.media_type.lower() != media_type:
                raise AttachmentInvalidError("声明的 MIME 与图片内容不一致")
            return self._new_ref(
                session_id,
                name,
                normalized,
                kind="image",
                media_type=media_type,
                width=width,
                height=height,
            ), normalized
        normalized, text_format = self._normalize_text(name, upload.media_type, data)
        media_type = next(
            (key for key, value in _TEXT_MEDIA.items() if value == text_format), "text/plain"
        )
        text = normalized.decode("utf-8")
        return self._new_ref(
            session_id,
            name,
            normalized,
            kind="text",
            media_type=media_type,
            encoding="utf-8",
            char_count=len(text),
            line_count=len(text.splitlines()),
            text_format=text_format,
        ), normalized

    def _normalize_image(self, data: bytes) -> tuple[bytes, str, int, int] | None:
        if len(data) > self._config.max_image_bytes:
            if data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF")):
                raise _too_large(
                    "图片字节数超过限制",
                    "image_bytes",
                    len(data),
                    self._config.max_image_bytes,
                )
            return None
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in _IMAGE_FORMATS:
                    return None
                if getattr(probe, "n_frames", 1) != 1:
                    raise AttachmentInvalidError("不支持动画图片")
                width, height = probe.size
                if (
                    width > self._config.max_image_edge
                    or height > self._config.max_image_edge
                    or width * height > self._config.max_image_pixels
                ):
                    raise _too_large(
                        "图片像素尺寸超过限制",
                        "image_pixels",
                        width * height,
                        self._config.max_image_pixels,
                    )
                probe.load()
                normalized_image = ImageOps.exif_transpose(probe)
                media_type, output_format = _IMAGE_FORMATS[probe.format]
                if output_format == "JPEG" and normalized_image.mode not in {"L", "RGB"}:
                    normalized_image = normalized_image.convert("RGB")
                output = io.BytesIO()
                normalized_image.save(output, format=output_format)
                normalized = output.getvalue()
                width, height = normalized_image.size
                return normalized, media_type, width, height
        except (UnidentifiedImageError, OSError):
            return None
        except Image.DecompressionBombError as exc:
            raise _too_large(
                "图片像素尺寸超过限制",
                "image_pixels",
                self._config.max_image_pixels + 1,
                self._config.max_image_pixels,
            ) from exc

    def _normalize_text(
        self, name: str, declared_media: str | None, data: bytes
    ) -> tuple[bytes, TextFormat]:
        if len(data) > self._config.max_text_bytes:
            raise _too_large(
                "文本附件字节数超过限制",
                "text_bytes",
                len(data),
                self._config.max_text_bytes,
            )
        suffix_format = _TEXT_SUFFIXES.get(Path(name).suffix.lower())
        media_format = _TEXT_MEDIA.get((declared_media or "").lower())
        if suffix_format is None and media_format is None:
            raise AttachmentInvalidError("附件不是支持的文本或图片类型")
        if suffix_format is not None and media_format is not None and suffix_format != media_format:
            raise AttachmentInvalidError("声明的 MIME 与文本格式不一致")
        if b"\x00" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise AttachmentInvalidError("文本附件包含二进制 NUL")
        try:
            if data.startswith(b"\xef\xbb\xbf"):
                text = data.decode("utf-8-sig")
            elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = data.decode("utf-16")
            else:
                text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttachmentInvalidError("文本附件编码不受支持") from exc
        if len(text) > self._config.max_text_chars:
            raise _too_large(
                "文本附件字符数超过限制",
                "text_chars",
                len(text),
                self._config.max_text_chars,
            )
        if any(ord(char) in _CONTROL_BYTES for char in text):
            raise AttachmentInvalidError("文本附件包含非法控制字符")
        return text.encode("utf-8"), media_format or suffix_format or "plain"

    @staticmethod
    def _new_ref(
        session_id: str,
        name: str,
        data: bytes,
        *,
        kind: Literal["image", "text"],
        media_type: str,
        **metadata: Any,
    ) -> AttachmentRefV1:
        return AttachmentRefV1(
            attachment_id=f"att_{secrets.token_hex(12)}",
            session_id=session_id,
            kind=kind,
            media_type=media_type,
            content_hash=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            display_name=name,
            created_at=_utc_now(),
            **metadata,
        )

    @staticmethod
    def _write_meta(item_dir: Path, ref: AttachmentRefV1, *, bound: bool) -> None:
        payload = {"ref": ref.model_dump(mode="json"), "bound": bound}
        temp = item_dir / ".meta.tmp"
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, item_dir / "meta.json")

    def _load(self, session_id: str, attachment_id: str) -> tuple[AttachmentRefV1, bytes, bool]:
        item_dir = self._item_dir(session_id, attachment_id)
        try:
            metadata = json.loads((item_dir / "meta.json").read_text(encoding="utf-8"))
            ref = AttachmentRefV1.model_validate(metadata["ref"], strict=True)
            data = (item_dir / "payload.bin").read_bytes()
        except FileNotFoundError as exc:
            raise AttachmentNotFoundError("Attachment 不存在") from exc
        except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise AttachmentUnavailableError("Attachment 存储不可用") from exc
        if ref.session_id != session_id or ref.attachment_id != attachment_id:
            raise AttachmentUnavailableError("Attachment ownership 校验失败")
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.content_hash:
            raise AttachmentUnavailableError("Attachment 完整性校验失败")
        return ref, data, metadata.get("bound") is True

    def get(self, ref: AttachmentRefV1) -> AttachmentPayloadV1:
        with self._lock:
            stored, data, _ = self._load(ref.session_id, ref.attachment_id)
            if stored != ref:
                raise AttachmentUnavailableError("Attachment 引用与存储不一致")
            return AttachmentPayloadV1(stored, data)

    def get_by_id(self, session_id: str, attachment_id: str) -> AttachmentPayloadV1:
        with self._lock:
            stored, data, _ = self._load(session_id, attachment_id)
            return AttachmentPayloadV1(stored, data)

    def bind(self, session_id: str, attachment_ids: Sequence[str]) -> None:
        with self._lock:
            loaded = [self._load(session_id, item) for item in dict.fromkeys(attachment_ids)]
            for ref, _data, bound in loaded:
                if not bound:
                    self._write_meta(self._item_dir(session_id, ref.attachment_id), ref, bound=True)

    def delete_unbound(self, session_id: str, attachment_ids: Sequence[str]) -> int:
        removed = 0
        with self._lock:
            for attachment_id in dict.fromkeys(attachment_ids):
                try:
                    _ref, _data, bound = self._load(session_id, attachment_id)
                except AttachmentNotFoundError:
                    continue
                if not bound:
                    shutil.rmtree(self._item_dir(session_id, attachment_id))
                    removed += 1
        return removed

    def collect_expired(self) -> int:
        cutoff = datetime.now(UTC).timestamp() - self._config.unbound_ttl_seconds
        removed = 0
        with self._lock:
            if not self._root.is_dir():
                return 0
            for item in self._root.glob("*/att_*"):
                try:
                    _ref, _data, bound = self._load(item.parent.name, item.name)
                    if not bound and (item / "meta.json").stat().st_mtime < cutoff:
                        shutil.rmtree(item)
                        removed += 1
                except (AttachmentNotFoundError, AttachmentUnavailableError, OSError):
                    continue
        return removed

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            shutil.rmtree(self._session_dir(session_id), ignore_errors=True)

    def fork(
        self,
        source_session_id: str,
        target_session_id: str,
        refs: Sequence[AttachmentRefV1],
    ) -> dict[str, AttachmentRefV1]:
        uploads: list[AttachmentUploadV1] = []
        ordered: list[AttachmentRefV1] = []
        for ref in refs:
            if ref.session_id != source_session_id:
                raise AttachmentInvalidError("fork Attachment 来源不一致")
            payload = self.get(ref)
            uploads.append(AttachmentUploadV1(payload.data, ref.display_name, ref.media_type))
            ordered.append(ref)
        summaries = self.ingest(target_session_id, uploads)
        result = {
            old.attachment_id: new.attachment for old, new in zip(ordered, summaries, strict=True)
        }
        self.bind(target_session_id, tuple(item.attachment_id for item in result.values()))
        return result
