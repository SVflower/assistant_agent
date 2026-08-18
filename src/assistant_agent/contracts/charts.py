"""当前受控图表 Artifact 与运行快照公共契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant_agent.contracts.charts_v2 import (
    ChartArtifactV2,
    ChartSpecV2,
    PresentationArtifactRefV2,
    build_chart_artifact_v2,
)
from assistant_agent.contracts.errors import UnsupportedChartSchemaError
from assistant_agent.contracts.failures import AllowedAction, BudgetSnapshot, RunFailure
from assistant_agent.contracts.observability import RunObservabilitySnapshot
from assistant_agent.contracts.outputs import OutputArtifactV1
from assistant_agent.contracts.presentation_common import (
    MAX_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACTS,
    canonical_json_bytes,
    stable_message_id,
)

CHART_SCHEMA_VERSION = 2


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _require_current_schema(value: Any) -> None:
    actual = value.get("schema_version") if isinstance(value, dict) else None
    if actual != CHART_SCHEMA_VERSION:
        raise UnsupportedChartSchemaError(
            f"Chart Artifact schema 不兼容：需要 v{CHART_SCHEMA_VERSION}",
            expected_version=CHART_SCHEMA_VERSION,
            actual_version=actual,
        )


def parse_chart_artifact(value: Any, *, strict: bool = True) -> ChartArtifactV2:
    _require_current_schema(value)
    return ChartArtifactV2.model_validate(value, strict=strict)


def parse_presentation_ref(value: Any, *, strict: bool = True) -> PresentationArtifactRefV2:
    _require_current_schema(value)
    return PresentationArtifactRefV2.model_validate(value, strict=strict)


class AssistantMessageSnapshot(_StrictModel):
    id: str | None = None
    role: Literal["assistant"] = "assistant"
    content: str = ""
    artifacts: tuple[PresentationArtifactRefV2, ...] = ()
    outputs: tuple[OutputArtifactV1, ...] = ()

    @field_validator("artifacts", "outputs", mode="before")
    @classmethod
    def _artifacts_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class PendingInteractionSnapshot(_StrictModel):
    request_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    call_id: str | None = None


class ExecutionModelSnapshot(_StrictModel):
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=300)


class RunSnapshot(_StrictModel):
    id: str
    session_id: str | None = None
    created_at: str
    execution_model: ExecutionModelSnapshot | None = None
    status: Literal["running", "paused", "cancelled", "completed", "failed"]
    phase: str
    updated_at: str
    preview: str
    terminal_status: Literal["completed", "failed", "paused", "cancelled"] | None = None
    failure: RunFailure | None = None
    current_phase: str | None = None
    budget: BudgetSnapshot
    pending_interaction: PendingInteractionSnapshot | None = None
    final_candidate: str | None = None
    artifacts: tuple[PresentationArtifactRefV2, ...] = ()
    outputs: tuple[OutputArtifactV1, ...] = ()
    allowed_actions: tuple[AllowedAction, ...] = ()
    execution_status: Literal["active", "inactive", "unknown"]
    retry_of_run_id: str | None = None
    observability: RunObservabilitySnapshot | None = None

    @field_validator("artifacts", "outputs", mode="before")
    @classmethod
    def _items_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


__all__ = [
    "CHART_SCHEMA_VERSION",
    "AssistantMessageSnapshot",
    "ChartArtifactV2",
    "ChartSpecV2",
    "ExecutionModelSnapshot",
    "MAX_ARTIFACT_BYTES",
    "MAX_RUN_ARTIFACT_BYTES",
    "MAX_RUN_ARTIFACTS",
    "PendingInteractionSnapshot",
    "PresentationArtifactRefV2",
    "RunSnapshot",
    "build_chart_artifact_v2",
    "canonical_json_bytes",
    "parse_chart_artifact",
    "parse_presentation_ref",
    "stable_message_id",
]
