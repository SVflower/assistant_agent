"""Session catalog 与元数据更新的稳定公共契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant_agent.contracts.time import parse_utc_timestamp

SESSION_CONTRACT_VERSION = 1


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


def _require_utc(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("时间必须是 ISO 8601 UTC")
    parsed = parse_utc_timestamp(value)
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("时间必须是 ISO 8601 UTC")
    return value
