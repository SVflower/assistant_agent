"""版本化用户消息 content-parts 与不可变 Attachment 引用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

ATTACHMENT_CONTRACT_VERSION = 1
CONTENT_PARTS_VERSION = 1
AttachmentKind = Literal["image", "text"]
TextFormat = Literal["plain", "markdown", "csv", "json", "log", "xml", "yaml"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AttachmentRefV1(_StrictModel):
    schema_version: Literal[1] = 1
    attachment_id: str = Field(pattern=r"^att_[a-f0-9]{24}$")
    session_id: str = Field(min_length=1)
    kind: AttachmentKind
    media_type: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    display_name: str = Field(min_length=1, max_length=200)
    created_at: str = Field(min_length=1)
    width: int | None = Field(default=None, ge=1, le=8192)
    height: int | None = Field(default=None, ge=1, le=8192)
    encoding: Literal["utf-8"] | None = None
    char_count: int | None = Field(default=None, ge=0, le=300_000)
    line_count: int | None = Field(default=None, ge=0)
    text_format: TextFormat | None = None

    @model_validator(mode="after")
    def _kind_metadata_matches(self) -> AttachmentRefV1:
        image_fields = (self.width, self.height)
        text_fields = (self.encoding, self.char_count, self.line_count, self.text_format)
        if self.kind == "image":
            if any(value is None for value in image_fields) or any(
                value is not None for value in text_fields
            ):
                raise ValueError("image Attachment 元数据不完整")
        elif any(value is not None for value in image_fields) or any(
            value is None for value in text_fields
        ):
            raise ValueError("text Attachment 元数据不完整")
        return self


class AttachmentSummaryV1(_StrictModel):
    schema_version: Literal[1] = 1
    attachment: AttachmentRefV1
    approximate_tokens: int | None = Field(default=None, ge=0)
    estimate_provider: str | None = None
    estimate_model: str | None = None


class TextPartV1(_StrictModel):
    type: Literal["text"] = "text"
    text: str


class AttachmentPartV1(_StrictModel):
    type: Literal["attachment"] = "attachment"
    attachment: AttachmentRefV1
    image_detail: Literal["auto", "low", "high"] = "auto"


ContentPartV1 = Annotated[TextPartV1 | AttachmentPartV1, Field(discriminator="type")]
_CONTENT_PART: TypeAdapter[ContentPartV1] = TypeAdapter(ContentPartV1)


class MessageContentV1(_StrictModel):
    schema_version: Literal[1] = 1
    parts: tuple[ContentPartV1, ...] = Field(min_length=1, max_length=16)

    @field_validator("parts", mode="before")
    @classmethod
    def _parts_to_tuple(cls, value: Any) -> Any:
        if isinstance(value, list):
            return tuple(_CONTENT_PART.validate_python(item, strict=True) for item in value)
        return value

    @model_validator(mode="after")
    def _attachment_limits(self) -> MessageContentV1:
        attachments = [part.attachment for part in self.parts if part.type == "attachment"]
        images = [item for item in attachments if item.kind == "image"]
        if len(attachments) > 8:
            raise ValueError("单消息附件不能超过 8 个")
        if len(images) > 4:
            raise ValueError("单消息图片不能超过 4 个")
        if sum({item.attachment_id: item.size_bytes for item in attachments}.values()) > 20 << 20:
            raise ValueError("单消息附件原始总量不能超过 20 MiB")
        if not attachments and not any(
            isinstance(part, TextPartV1) and part.text.strip() for part in self.parts
        ):
            raise ValueError("用户消息不能是空内容")
        return self

    def text(self) -> str:
        return "\n".join(part.text for part in self.parts if isinstance(part, TextPartV1)).strip()

    def attachment_refs(self) -> tuple[AttachmentRefV1, ...]:
        return tuple(part.attachment for part in self.parts if isinstance(part, AttachmentPartV1))

    def safe_preview(self) -> str:
        text = " ".join(self.text().split())
        labels = [f"[{item.kind}: {item.display_name}]" for item in self.attachment_refs()]
        return " ".join(part for part in (text, *labels) if part)


class UserMessageInputV1(_StrictModel):
    schema_version: Literal[1] = 1
    content: MessageContentV1

    @classmethod
    def from_text(cls, text: str) -> UserMessageInputV1:
        return cls(content=MessageContentV1(parts=(TextPartV1(text=text),)))


def parse_message_content(value: object) -> MessageContentV1:
    if isinstance(value, MessageContentV1):
        return value
    return MessageContentV1.model_validate(value, strict=True)


def content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return parse_message_content(value).text()


def attachment_token_estimate(value: object, *, image_reserve: int) -> int:
    """Conservative provider-independent cost used before payload materialization."""
    content = parse_message_content(value)
    total = 0
    seen: set[str] = set()
    for ref in content.attachment_refs():
        if ref.attachment_id in seen:
            continue
        seen.add(ref.attachment_id)
        total += image_reserve if ref.kind == "image" else max(1, (ref.char_count or 0) // 4)
    return total


def remap_content_attachments(
    value: object, mapping: dict[str, AttachmentRefV1]
) -> MessageContentV1:
    content = parse_message_content(value)
    parts: list[ContentPartV1] = []
    for part in content.parts:
        if isinstance(part, AttachmentPartV1):
            replacement = mapping.get(part.attachment.attachment_id)
            if replacement is None:
                raise ValueError("Attachment fork 映射不完整")
            parts.append(part.model_copy(update={"attachment": replacement}))
        else:
            parts.append(part)
    return MessageContentV1(parts=tuple(parts))


@dataclass(frozen=True)
class AttachmentUploadV1:
    data: bytes
    display_name: str
    media_type: str | None = None


@dataclass(frozen=True)
class AttachmentPayloadV1:
    ref: AttachmentRefV1
    data: bytes
