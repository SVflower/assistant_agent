"""可恢复且不包含隐藏推理的 Run 观测公共契约。"""

from __future__ import annotations

import math
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OBSERVABILITY_CONTRACT_VERSION: Literal[1] = 1
MAX_TRAJECTORY_ENTRIES = 256

MetricSource: TypeAlias = Literal["provider", "estimated", "derived", "unavailable"]
TrajectoryCategory: TypeAlias = Literal[
    "run", "model", "tool", "interaction", "output", "compaction"
]
TrajectoryStatus: TypeAlias = Literal[
    "started", "streaming", "waiting", "paused", "completed", "failed", "cancelled"
]
TaskPlanStatus: TypeAlias = Literal["pending", "in_progress", "completed"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TimingSnapshot(_StrictModel):
    run_started_at: str = Field(min_length=1)
    completed_at: str | None = None
    run_duration_ms: int | None = Field(default=None, ge=0)
    model_duration_ms: int | None = Field(default=None, ge=0)
    tool_duration_ms: int | None = Field(default=None, ge=0)
    interaction_wait_duration_ms: int | None = Field(default=None, ge=0)
    first_token_latency_ms: int | None = Field(default=None, ge=0)
    tokens_per_second: float | None = Field(default=None, ge=0)
    source: MetricSource = "unavailable"

    @field_validator("tokens_per_second")
    @classmethod
    def _finite_rate(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("tokens_per_second 必须是有限数")
        return value


class OrchestrationTimingSnapshot(_StrictModel):
    """Agent 自身编排与持久化开销；缺少稳定来源的值保持 ``None``。"""

    context_build_duration_ms: int | None = Field(default=None, ge=0)
    checkpoint_count: int | None = Field(default=None, ge=0)
    checkpoint_duration_ms: int | None = Field(default=None, ge=0)
    checkpoint_bytes: int | None = Field(default=None, ge=0)
    session_sync_duration_ms: int | None = Field(default=None, ge=0)
    source: Literal["derived", "unavailable"] = "unavailable"


class ContextUsageSnapshot(_StrictModel):
    used_tokens: int | None = Field(default=None, ge=0)
    projected_tokens: int | None = Field(default=None, ge=0)
    limit_tokens: int | None = Field(default=None, gt=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    source: Literal["provider", "estimated", "unavailable"] = "unavailable"

    @field_validator("percent")
    @classmethod
    def _finite_percent(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("context percent 必须是有限数")
        return value

    @model_validator(mode="after")
    def _source_matches_value(self) -> ContextUsageSnapshot:
        if self.source == "unavailable" and self.used_tokens is not None:
            raise ValueError("unavailable context 不得携带 used_tokens")
        if self.source != "unavailable" and self.used_tokens is None:
            raise ValueError("可用 context source 必须携带 used_tokens")
        return self


class ModelUsageSnapshot(_StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_hit_percent: float | None = Field(default=None, ge=0, le=100)
    token_source: Literal["provider", "unavailable"] = "unavailable"
    cache_source: Literal["provider", "unavailable"] = "unavailable"
    performance_source: Literal["derived", "unavailable"] = "unavailable"

    @field_validator("cache_hit_percent")
    @classmethod
    def _finite_percent(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("cache_hit_percent 必须是有限数")
        return value

    @model_validator(mode="after")
    def _sources_match_values(self) -> ModelUsageSnapshot:
        if self.token_source == "unavailable" and (
            self.input_tokens is not None or self.output_tokens is not None
        ):
            raise ValueError("unavailable token source 不得携带 token")
        cache_known = self.cache_read_tokens is not None or self.cache_write_tokens is not None
        if self.cache_source == "unavailable" and cache_known:
            raise ValueError("unavailable cache source 不得携带 cache token")
        return self


class TrajectoryEntry(_StrictModel):
    entry_id: str = Field(pattern=r"^traj_[a-f0-9]{24}$")
    sequence: int = Field(ge=1)
    category: TrajectoryCategory
    status: TrajectoryStatus
    title: str = Field(min_length=1, max_length=160)
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    call_id: str | None = Field(default=None, max_length=200)
    tool_name: str | None = Field(default=None, max_length=160)
    result_code: str | None = Field(default=None, max_length=160)
    summary: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _completion_is_consistent(self) -> TrajectoryEntry:
        finished = self.status in {"paused", "completed", "failed", "cancelled"}
        if finished and self.completed_at is None:
            raise ValueError("结束轨迹必须有 completed_at")
        if not finished and (self.completed_at is not None or self.duration_ms is not None):
            raise ValueError("未结束轨迹不得有完成时间或耗时")
        return self


class TaskPlanItem(_StrictModel):
    item_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=200)
    status: TaskPlanStatus


class TaskPlanSnapshot(_StrictModel):
    revision: int = Field(ge=1)
    updated_at: str = Field(min_length=1)
    items: tuple[TaskPlanItem, ...] = Field(max_length=32)

    @field_validator("items", mode="before")
    @classmethod
    def _items_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_items(self) -> TaskPlanSnapshot:
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("task plan item_id 不得重复")
        return self


class RunObservabilitySnapshot(_StrictModel):
    schema_version: Literal[1] = OBSERVABILITY_CONTRACT_VERSION
    timing: TimingSnapshot
    context: ContextUsageSnapshot
    model_usage: ModelUsageSnapshot
    orchestration: OrchestrationTimingSnapshot = Field(default_factory=OrchestrationTimingSnapshot)
    trajectory: tuple[TrajectoryEntry, ...] = Field(max_length=MAX_TRAJECTORY_ENTRIES)
    task_plan: TaskPlanSnapshot | None = None
    truncated: bool = False

    @field_validator("trajectory", mode="before")
    @classmethod
    def _trajectory_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _trajectory_is_ordered(self) -> RunObservabilitySnapshot:
        sequences = [entry.sequence for entry in self.trajectory]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("trajectory sequence 必须严格递增且唯一")
        return self


__all__ = [
    "MAX_TRAJECTORY_ENTRIES",
    "OBSERVABILITY_CONTRACT_VERSION",
    "ContextUsageSnapshot",
    "MetricSource",
    "ModelUsageSnapshot",
    "OrchestrationTimingSnapshot",
    "RunObservabilitySnapshot",
    "TaskPlanItem",
    "TaskPlanSnapshot",
    "TimingSnapshot",
    "TrajectoryCategory",
    "TrajectoryEntry",
    "TrajectoryStatus",
]
