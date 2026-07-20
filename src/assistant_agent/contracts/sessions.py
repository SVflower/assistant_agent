"""Session catalog 与元数据更新的稳定公共契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant_agent.contracts.charts import (
    AssistantMessageSnapshot,
    PresentationArtifactRef,
)
from assistant_agent.contracts.time import parse_utc_timestamp

SESSION_CONTRACT_VERSION = 2


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LastRunSummary(_StrictModel):
    id: str = Field(min_length=1)
    status: Literal["running", "paused", "cancelled", "completed", "failed"]
    updated_at: str = Field(min_length=1)

    @field_validator("updated_at")
    @classmethod
    def _time_is_utc(cls, value: str) -> str:
        return _require_utc(value)


class SessionSummary(_StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=100)
    title_source: Literal["auto", "user"]
    metadata_version: int = Field(ge=1)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    message_count: int = Field(ge=0)
    preview: str
    last_run: LastRunSummary | None = None

    @field_validator("title")
    @classmethod
    def _title_has_visible_text(cls, value: str) -> str:
        if not any(not char.isspace() for char in value):
            raise ValueError("title 必须包含非空白字符")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _times_are_utc(cls, value: str) -> str:
        return _require_utc(value)


class SessionCatalogPage(_StrictModel):
    items: tuple[SessionSummary, ...] = ()
    next_cursor: str | None = None

    @field_validator("items", mode="before")
    @classmethod
    def _items_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class UpdateSessionMetadataRequest(_StrictModel):
    title: str = Field(min_length=1, max_length=100)
    expected_metadata_version: int = Field(ge=1)

    @field_validator("title")
    @classmethod
    def _title_has_visible_text(cls, value: str) -> str:
        if not any(not char.isspace() for char in value):
            raise ValueError("title 必须包含非空白字符")
        return value


class PublicMessageSnapshot(_StrictModel):
    id: str = Field(pattern=r"^msg_[a-f0-9]{24}$")
    role: Literal["user", "assistant"]
    created_at: str | None = None
    reply_to_message_id: str | None = Field(default=None, pattern=r"^msg_[a-f0-9]{24}$")
    content: str = ""
    artifacts: tuple[PresentationArtifactRef, ...] = ()

    @field_validator("created_at")
    @classmethod
    def _optional_time_is_utc(cls, value: str | None) -> str | None:
        return _require_utc(value) if value is not None else None

    @field_validator("artifacts", mode="before")
    @classmethod
    def _artifacts_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _reply_matches_role(self) -> PublicMessageSnapshot:
        if self.role == "user" and self.reply_to_message_id is not None:
            raise ValueError("user message 的 reply_to_message_id 必须为 null")
        if self.role == "assistant" and self.reply_to_message_id is None:
            raise ValueError("assistant message 必须指向对应 user message")
        return self


class SessionSnapshot(_StrictModel):
    id: str = Field(min_length=1)
    schema_version: Literal[2] = 2
    title: str = Field(default="（空会话）", min_length=1, max_length=100)
    title_source: Literal["auto", "user"] = "auto"
    metadata_version: int = Field(default=1, ge=1)
    created_at: str | None = None
    updated_at: str | None = None
    messages: tuple[PublicMessageSnapshot, ...] = ()
    artifacts: tuple[PresentationArtifactRef, ...] = ()
    assistant_messages: tuple[AssistantMessageSnapshot, ...] = ()
    fork_created: bool | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _optional_times_are_utc(cls, value: str | None) -> str | None:
        return _require_utc(value) if value is not None else None

    @field_validator("messages", "artifacts", "assistant_messages", mode="before")
    @classmethod
    def _items_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _ledger_is_self_contained(self) -> SessionSnapshot:
        roles = {message.id: message.role for message in self.messages}
        if len(roles) != len(self.messages):
            raise ValueError("Session message ID 必须唯一")
        for message in self.messages:
            if (
                message.reply_to_message_id is not None
                and roles.get(message.reply_to_message_id) != "user"
            ):
                raise ValueError("assistant reply 必须指向同一 Session user message")
            if any(
                ref.session_id != self.id or ref.message_id != message.id
                for ref in message.artifacts
            ):
                raise ValueError("message Artifact 归属不一致")
        if any(ref.session_id != self.id for ref in self.artifacts):
            raise ValueError("Session Artifact 归属不一致")
        return self


def _require_utc(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("时间必须是 ISO 8601 UTC")
    parsed = parse_utc_timestamp(value)
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("时间必须是 ISO 8601 UTC")
    return value
