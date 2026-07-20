"""受控图表规格与不可变展示 Artifact 公共契约。"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant_agent.contracts.failures import AllowedAction, BudgetSnapshot, RunFailure

MAX_ARTIFACT_BYTES = 512 * 1024
MAX_RUN_ARTIFACTS = 16
MAX_RUN_ARTIFACT_BYTES = 2 * 1024 * 1024

ChartType = Literal["line", "bar", "stacked_bar", "area", "scatter", "donut"]
DataType = Literal["string", "number", "datetime"]
ChartCell = str | int | float | None


def canonical_json_bytes(value: Any) -> bytes:
    """按冻结契约生成稳定 UTF-8 canonical JSON。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ChartColumn(_StrictModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    data_type: DataType
    unit: str | None = Field(default=None, max_length=128)


class ChartSeries(_StrictModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)


class ChartSpecV1(_StrictModel):
    schema_version: Literal[1] = 1
    chart_type: ChartType
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    source_label: str | None = Field(default=None, max_length=500)
    columns: tuple[ChartColumn, ...] = Field(min_length=1, max_length=12)
    rows: tuple[tuple[ChartCell, ...], ...] = Field(default=(), max_length=5000)
    x_key: str | None = Field(default=None, max_length=64)
    series: tuple[ChartSeries, ...] = Field(default=(), max_length=8)
    category_key: str | None = Field(default=None, max_length=64)
    value_key: str | None = Field(default=None, max_length=64)

    @field_validator("columns", "series", mode="before")
    @classmethod
    def _arrays_to_tuples(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("rows", mode="before")
    @classmethod
    def _rows_to_tuples(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return tuple(tuple(row) if isinstance(row, list) else row for row in value)

    @model_validator(mode="after")
    def _validate_encoding_and_data(self) -> ChartSpecV1:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("column key 必须唯一")
        width = len(self.columns)
        if len(self.rows) * width > 20_000:
            raise ValueError("图表单元格不能超过 20000")
        for row in self.rows:
            if len(row) != width:
                raise ValueError("每行单元格数量必须与 columns 一致")
            for column, cell in zip(self.columns, row, strict=True):
                if cell is None:
                    continue
                if isinstance(cell, bool):
                    raise ValueError("布尔值不是合法图表数字")
                if isinstance(cell, float) and not math.isfinite(cell):
                    raise ValueError("图表数字必须是有限值")
                if column.data_type == "number" and not isinstance(cell, (int, float)):
                    raise ValueError(f"number 列 {column.key} 只能包含数字或 null")
                if column.data_type in {"string", "datetime"} and not isinstance(cell, str):
                    raise ValueError(f"{column.data_type} 列 {column.key} 只能包含字符串或 null")

        types = {column.key: column.data_type for column in self.columns}
        series_keys = [item.key for item in self.series]
        if len(series_keys) != len(set(series_keys)):
            raise ValueError("series key 必须唯一")
        if any(key not in types for key in series_keys):
            raise ValueError("series 必须引用已声明列")

        if self.chart_type == "donut":
            if self.x_key is not None or self.series:
                raise ValueError("donut 不能设置 x_key 或 series")
            if self.category_key not in types or self.value_key not in types:
                raise ValueError("donut 必须设置有效 category_key 和 value_key")
            if types[self.value_key] != "number":
                raise ValueError("donut value_key 必须引用 number 列")
        else:
            if self.category_key is not None or self.value_key is not None:
                raise ValueError("非 donut 图表不能设置 category_key/value_key")
            if self.x_key not in types or not self.series:
                raise ValueError("图表必须设置有效 x_key 和至少一个 series")
            if self.chart_type == "scatter" and (
                types[self.x_key] != "number" or any(types[key] != "number" for key in series_keys)
            ):
                raise ValueError("scatter 的 x_key 和 series 必须引用 number 列")
        return self


class PresentationArtifactRef(_StrictModel):
    artifact_id: str = Field(pattern=r"^chart_[a-f0-9]{24}$")
    kind: Literal["chart"] = "chart"
    schema_version: Literal[1] = 1
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    session_id: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{24}$")
    created_at: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)


class ChartArtifact(PresentationArtifactRef):
    spec: ChartSpecV1

    @model_validator(mode="after")
    def _integrity_matches_payload(self) -> ChartArtifact:
        expected_hash = (
            "sha256:"
            + hashlib.sha256(canonical_json_bytes(self.spec.model_dump(mode="json"))).hexdigest()
        )
        if self.content_hash != expected_hash:
            raise ValueError("Artifact content_hash 与 spec 不一致")
        actual_size = len(canonical_json_bytes(self.model_dump(mode="json")))
        if self.size_bytes != actual_size:
            raise ValueError("Artifact size_bytes 与载荷不一致")
        if self.run_id is not None and self.message_id != stable_message_id(self.run_id):
            raise ValueError("Artifact message_id 与 run_id 不一致")
        return self

    @property
    def ref(self) -> PresentationArtifactRef:
        return PresentationArtifactRef.model_validate(
            self.model_dump(exclude={"spec"}), strict=True
        )


class AssistantMessageSnapshot(_StrictModel):
    id: str | None = None
    role: Literal["assistant"] = "assistant"
    content: str = ""
    artifacts: tuple[PresentationArtifactRef, ...] = ()

    @field_validator("artifacts", mode="before")
    @classmethod
    def _artifacts_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class PendingInteractionSnapshot(_StrictModel):
    request_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    call_id: str | None = None


class RunSnapshot(_StrictModel):
    id: str
    session_id: str | None = None
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
    artifacts: tuple[PresentationArtifactRef, ...] = ()
    allowed_actions: tuple[AllowedAction, ...] = ()
    execution_status: Literal["active", "inactive", "unknown"]
    retry_of_run_id: str | None = None

    @field_validator("artifacts", mode="before")
    @classmethod
    def _items_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


def stable_message_id(run_id: str) -> str:
    digest = hashlib.sha256(f"message:{run_id}".encode()).hexdigest()[:24]
    return f"msg_{digest}"


def build_chart_artifact(
    spec: ChartSpecV1,
    *,
    session_id: str,
    run_id: str,
    call_id: str,
    created_at: str | None = None,
) -> ChartArtifact:
    """构造确定性 ID/哈希并校验最终 Artifact JSON 硬限。"""
    spec_data = spec.model_dump(mode="json")
    content_hash = "sha256:" + hashlib.sha256(canonical_json_bytes(spec_data)).hexdigest()
    identity = canonical_json_bytes([session_id, run_id, call_id, content_hash])
    artifact_id = "chart_" + hashlib.sha256(identity).hexdigest()[:24]
    base = {
        "artifact_id": artifact_id,
        "kind": "chart",
        "schema_version": 1,
        "content_hash": content_hash,
        "session_id": session_id,
        "run_id": run_id,
        "message_id": stable_message_id(run_id),
        "created_at": created_at
        or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "title": spec.title,
        "spec": spec_data,
    }
    size = 1
    for _ in range(4):
        size = len(canonical_json_bytes({**base, "size_bytes": size}))
    if size > MAX_ARTIFACT_BYTES:
        raise ValueError("单个图表 Artifact 超过 512 KiB")
    artifact = ChartArtifact.model_validate({**base, "size_bytes": size}, strict=True)
    return artifact
