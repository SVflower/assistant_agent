"""Managed Output 公共契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant_agent.contracts.time import parse_utc_timestamp

OUTPUT_CONTRACT_VERSION = 1
OutputDisposition = Literal["inline", "download"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class OutputArtifactV1(_StrictModel):
    """可安全跨进程传递的输出引用；绝不包含服务器路径。"""

    schema_version: Literal[1] = 1
    output_id: str = Field(pattern=r"^out_[a-f0-9]{32}$")
    session_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    message_id: str | None = Field(default=None, pattern=r"^msg_[a-f0-9]{24}$")
    call_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=180)
    title: str | None = Field(default=None, max_length=200)
    media_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str = Field(min_length=1)
    disposition: OutputDisposition = "download"
    preview_supported: bool = False

    @field_validator("created_at")
    @classmethod
    def _time_is_utc(cls, value: str) -> str:
        parsed = parse_utc_timestamp(value)
        if not value.endswith("Z") or parsed.utcoffset() is None:
            raise ValueError("created_at 必须是 ISO 8601 UTC")
        return value


class OutputPayload(_StrictModel):
    """首版文本输出载荷；binary producer 后续通过独立可信端口扩展。"""

    artifact: OutputArtifactV1
    content: str


class OutputError(RuntimeError):
    code = "output_error"


class OutputInvalidError(OutputError):
    code = "output_invalid"


class OutputLimitExceededError(OutputError):
    code = "output_limit_exceeded"


class OutputNotFoundError(OutputError):
    code = "output_not_found"


class OutputConflictError(OutputError):
    code = "output_conflict"


class OutputUnavailableError(OutputError):
    code = "output_unavailable"


__all__ = [
    "OUTPUT_CONTRACT_VERSION",
    "OutputArtifactV1",
    "OutputConflictError",
    "OutputDisposition",
    "OutputError",
    "OutputInvalidError",
    "OutputLimitExceededError",
    "OutputNotFoundError",
    "OutputPayload",
    "OutputUnavailableError",
]
