"""L4 高频普通图表的版本化、受控 ChartSpecV2。"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant_agent.contracts.datasets import DatasetCell, TabularDatasetV1
from assistant_agent.contracts.presentation_common import (
    MAX_ARTIFACT_BYTES,
    canonical_json_bytes,
    stable_message_id,
)

ChartTypeV2 = Literal[
    "line",
    "area",
    "bar",
    "grouped_bar",
    "stacked_bar",
    "percent_stacked_bar",
    "pie",
    "donut",
    "combo_bar_line",
    "dual_axis",
    "scatter",
    "bubble",
    "histogram",
    "boxplot",
    "heatmap",
]
SeriesMark = Literal[
    "line", "area", "bar", "scatter", "bubble", "pie", "donut", "boxplot", "heatmap"
]
AxisScale = Literal["category", "linear", "time"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AxisSpecV1(_StrictModel):
    axis_id: str = Field(pattern=r"^axis_[a-z0-9_]{1,32}$")
    dimension: Literal["x", "y"]
    scale: AxisScale
    position: Literal["bottom", "left", "right"]
    title: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _position_matches_dimension(self) -> AxisSpecV1:
        if self.dimension == "x" and self.position != "bottom":
            raise ValueError("X 轴只能位于 bottom")
        if self.dimension == "y" and self.position not in {"left", "right"}:
            raise ValueError("Y 轴只能位于 left/right")
        return self


class SeriesSpecV1(_StrictModel):
    series_id: str = Field(pattern=r"^series_[a-z0-9_]{1,32}$")
    label: str = Field(min_length=1, max_length=128)
    mark: SeriesMark
    dataset_id: str = Field(pattern=r"^ds_[a-z0-9_]{1,48}$")
    x_key: str | None = Field(default=None, max_length=64)
    y_key: str | None = Field(default=None, max_length=64)
    category_key: str | None = Field(default=None, max_length=64)
    value_key: str | None = Field(default=None, max_length=64)
    size_key: str | None = Field(default=None, max_length=64)
    min_key: str | None = Field(default=None, max_length=64)
    q1_key: str | None = Field(default=None, max_length=64)
    median_key: str | None = Field(default=None, max_length=64)
    q3_key: str | None = Field(default=None, max_length=64)
    max_key: str | None = Field(default=None, max_length=64)
    outlier_dataset_id: str | None = Field(default=None, max_length=52)
    x_axis_id: str | None = Field(default=None, max_length=40)
    y_axis_id: str | None = Field(default=None, max_length=40)
    stack_id: str | None = Field(default=None, max_length=32)


class ReferenceLineSpecV1(_StrictModel):
    axis_id: str = Field(max_length=40)
    value: DatasetCell
    label: str = Field(min_length=1, max_length=128)


class ReferenceBandSpecV1(_StrictModel):
    axis_id: str = Field(max_length=40)
    start: DatasetCell
    end: DatasetCell
    label: str = Field(min_length=1, max_length=128)


class ErrorBarSpecV1(_StrictModel):
    series_id: str = Field(max_length=40)
    lower_key: str = Field(min_length=1, max_length=64)
    upper_key: str = Field(min_length=1, max_length=64)


class AnnotationSpecV1(_StrictModel):
    text: str = Field(min_length=1, max_length=200)
    x_value: DatasetCell = None
    y_value: int | float | None = None
    series_id: str | None = Field(default=None, max_length=40)

    @field_validator("y_value")
    @classmethod
    def _finite_y(cls, value: int | float | None) -> int | float | None:
        if isinstance(value, bool) or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("annotation y_value 必须为有限数字")
        return value


class ChartPanelV1(_StrictModel):
    panel_id: str = Field(pattern=r"^panel_[a-z0-9_]{1,32}$")
    title: str | None = Field(default=None, max_length=160)
    chart_type: ChartTypeV2
    x_axis: AxisSpecV1 | None = None
    y_axes: tuple[AxisSpecV1, ...] = Field(default=(), max_length=2)
    series: tuple[SeriesSpecV1, ...] = Field(min_length=1, max_length=8)
    reference_lines: tuple[ReferenceLineSpecV1, ...] = Field(default=(), max_length=16)
    reference_bands: tuple[ReferenceBandSpecV1, ...] = Field(default=(), max_length=16)
    error_bars: tuple[ErrorBarSpecV1, ...] = Field(default=(), max_length=16)
    annotations: tuple[AnnotationSpecV1, ...] = Field(default=(), max_length=32)

    @field_validator(
        "y_axes",
        "series",
        "reference_lines",
        "reference_bands",
        "error_bars",
        "annotations",
        mode="before",
    )
    @classmethod
    def _items_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class ChartLayoutV1(_StrictModel):
    columns: int = Field(default=1, ge=1, le=2)
    panel_order: tuple[str, ...] = Field(min_length=1, max_length=4)
    shared_legend: bool = True

    @field_validator("panel_order", mode="before")
    @classmethod
    def _order_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class DerivationTraceV1(_StrictModel):
    kind: Literal["histogram", "boxplot", "percent", "aggregate"]
    algorithm: Literal[
        "explicit_bins_v1",
        "freedman_diaconis_v1",
        "sturges_v1",
        "type7_iqr_v1",
        "category_percent_v1",
        "aggregate_v1",
    ]
    source_dataset_id: str = Field(max_length=52)
    output_dataset_id: str = Field(max_length=52)
    value_key: str = Field(max_length=64)
    group_key: str | None = Field(default=None, max_length=64)
    parameter: str | None = Field(default=None, max_length=128)


class ChartSpecV2(_StrictModel):
    schema_version: Literal[2] = 2
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    source_label: str | None = Field(default=None, max_length=500)
    datasets: tuple[TabularDatasetV1, ...] = Field(min_length=1, max_length=4)
    layout: ChartLayoutV1
    panels: tuple[ChartPanelV1, ...] = Field(min_length=1, max_length=4)
    derivations: tuple[DerivationTraceV1, ...] = ()

    @field_validator("datasets", "panels", "derivations", mode="before")
    @classmethod
    def _items_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_graph(self) -> ChartSpecV2:
        dataset_map = {item.dataset_id: item for item in self.datasets}
        if len(dataset_map) != len(self.datasets):
            raise ValueError("dataset_id 必须唯一")
        if sum(len(item.columns) * len(item.rows) for item in self.datasets) > 20_000:
            raise ValueError("全部 source/derived dataset 不能超过 20000 cells")
        panel_map = {item.panel_id: item for item in self.panels}
        if len(panel_map) != len(self.panels):
            raise ValueError("panel_id 必须唯一")
        if tuple(self.layout.panel_order) != tuple(item.panel_id for item in self.panels):
            raise ValueError("layout.panel_order 必须完整匹配 panels 顺序")
        all_series = [series for panel in self.panels for series in panel.series]
        if len(all_series) > 8:
            raise ValueError("全 Artifact series 不能超过 8")
        if len({item.series_id for item in all_series}) != len(all_series):
            raise ValueError("series_id 必须在 Artifact 内唯一")
        for trace in self.derivations:
            if (
                trace.source_dataset_id not in dataset_map
                or trace.output_dataset_id not in dataset_map
            ):
                raise ValueError("derivation 必须引用已声明 source/output dataset")
            if trace.source_dataset_id == trace.output_dataset_id:
                raise ValueError("derivation source/output dataset 不能相同")
        if len({item.output_dataset_id for item in self.derivations}) != len(self.derivations):
            raise ValueError("每个 derived dataset 只能有一条 derivation")
        for panel in self.panels:
            self._validate_panel(panel, dataset_map)
        return self

    @staticmethod
    def _validate_panel(panel: ChartPanelV1, datasets: dict[str, TabularDatasetV1]) -> None:
        axes = ([panel.x_axis] if panel.x_axis is not None else []) + list(panel.y_axes)
        axis_map = {item.axis_id: item for item in axes}
        if len(axis_map) != len(axes):
            raise ValueError("panel axis_id 必须唯一")
        if len({axis.position for axis in panel.y_axes}) != len(panel.y_axes):
            raise ValueError("同一 panel 的 Y 轴位置必须唯一")
        series_map = {item.series_id: item for item in panel.series}
        _validate_chart_type(panel)
        for panel_series in panel.series:
            dataset = datasets.get(panel_series.dataset_id)
            if dataset is None:
                raise ValueError("series 必须引用已声明 dataset")
            _validate_series(panel_series, dataset, axis_map)
            if panel_series.outlier_dataset_id is not None:
                outliers = datasets.get(panel_series.outlier_dataset_id)
                if (
                    panel_series.mark != "boxplot"
                    or outliers is None
                    or outliers.column_type("value") != "number"
                    or outliers.column_type("group") != "string"
                ):
                    raise ValueError("boxplot outlier dataset 无效")
        for line in panel.reference_lines:
            _validate_axis_value(axis_map, line.axis_id, line.value)
        for band in panel.reference_bands:
            _validate_axis_value(axis_map, band.axis_id, band.start)
            _validate_axis_value(axis_map, band.axis_id, band.end)
            if (
                isinstance(band.start, (int, float))
                and not isinstance(band.start, bool)
                and isinstance(band.end, (int, float))
                and not isinstance(band.end, bool)
                and band.start >= band.end
            ):
                raise ValueError("reference band start 必须小于 end")
        for error_bar in panel.error_bars:
            series = series_map.get(error_bar.series_id)
            if series is None or series.y_key is None:
                raise ValueError("error bar 必须引用数值 series")
            dataset = datasets[series.dataset_id]
            if any(
                dataset.column_type(key) != "number"
                for key in (series.y_key, error_bar.lower_key, error_bar.upper_key)
            ):
                raise ValueError("error bar 的 value/lower/upper 必须为 number 列")
            indices = {column.key: index for index, column in enumerate(dataset.columns)}
            for row in dataset.rows:
                value, lower, upper = (
                    row[indices[key]]
                    for key in (series.y_key, error_bar.lower_key, error_bar.upper_key)
                )
                if all(
                    isinstance(cell, (int, float)) and not isinstance(cell, bool)
                    for cell in (value, lower, upper)
                ) and not float(cast(int | float, lower)) <= float(
                    cast(int | float, value)
                ) <= float(cast(int | float, upper)):
                    raise ValueError("error bar 必须满足 lower <= value <= upper")
        if any(
            annotation.series_id is not None and annotation.series_id not in series_map
            for annotation in panel.annotations
        ):
            raise ValueError("annotation series_id 必须引用当前 panel 的 series")


def _validate_chart_type(panel: ChartPanelV1) -> None:
    marks = [item.mark for item in panel.series]
    chart_type = panel.chart_type
    exact_mark = {
        "line": "line",
        "area": "area",
        "bar": "bar",
        "grouped_bar": "bar",
        "stacked_bar": "bar",
        "percent_stacked_bar": "bar",
        "scatter": "scatter",
        "bubble": "bubble",
        "pie": "pie",
        "donut": "donut",
        "histogram": "bar",
        "boxplot": "boxplot",
        "heatmap": "heatmap",
    }.get(chart_type)
    if exact_mark is not None and any(mark != exact_mark for mark in marks):
        raise ValueError("chart_type 与 series mark 不一致")
    if chart_type in {"bar", "bubble", "pie", "donut", "histogram", "boxplot", "heatmap"}:
        if len(marks) != 1:
            raise ValueError(f"{chart_type} 只能包含一个 series")
    if chart_type in {"grouped_bar", "stacked_bar", "percent_stacked_bar"} and len(marks) < 2:
        raise ValueError(f"{chart_type} 至少包含两个 series")
    if chart_type == "grouped_bar" and any(item.stack_id is not None for item in panel.series):
        raise ValueError("grouped_bar 不得设置 stack_id")
    if chart_type in {"stacked_bar", "percent_stacked_bar"}:
        stacks = {item.stack_id for item in panel.series}
        if None in stacks or len(stacks) != 1:
            raise ValueError(f"{chart_type} 的 series 必须使用同一个 stack_id")
    if chart_type == "combo_bar_line" and not {"bar", "line"} <= set(marks):
        raise ValueError("combo_bar_line 必须同时包含 bar 和 line")
    if chart_type == "dual_axis":
        axis_ids = {item.y_axis_id for item in panel.series}
        if len(panel.y_axes) != 2 or axis_ids != {"axis_y", "axis_y2"}:
            raise ValueError("dual_axis 必须同时引用左右两个 Y 轴")
    if chart_type in {"pie", "donut"} and (panel.x_axis is not None or panel.y_axes):
        raise ValueError("pie/donut 不得声明坐标轴")


def _validate_axis_value(axes: dict[str, AxisSpecV1], axis_id: str, value: DatasetCell) -> None:
    axis = axes.get(axis_id)
    if axis is None:
        raise ValueError("overlay 必须引用已声明 axis")
    if value is None or isinstance(value, bool):
        raise ValueError("overlay value 不能为空或布尔值")
    if axis.scale == "linear" and not isinstance(value, (int, float)):
        raise ValueError("linear axis overlay 必须为数字")
    if axis.scale in {"category", "time"} and not isinstance(value, str):
        raise ValueError("category/time axis overlay 必须为字符串")


def _validate_series(
    series: SeriesSpecV1, dataset: TabularDatasetV1, axes: dict[str, AxisSpecV1]
) -> None:
    keys = {column.key for column in dataset.columns}
    referenced = {
        key
        for key in (
            series.x_key,
            series.y_key,
            series.category_key,
            series.value_key,
            series.size_key,
            series.min_key,
            series.q1_key,
            series.median_key,
            series.q3_key,
            series.max_key,
        )
        if key is not None
    }
    if not referenced <= keys:
        raise ValueError("series 字段必须引用已声明列")
    if series.mark in {"line", "area", "bar", "scatter"}:
        if series.x_key is None or series.y_key is None:
            raise ValueError("cartesian series 必须设置 x_key/y_key")
    elif series.mark == "bubble":
        if series.x_key is None or series.y_key is None or series.size_key is None:
            raise ValueError("bubble 必须设置 x_key/y_key/size_key")
        if dataset.column_type(series.size_key) != "number":
            raise ValueError("bubble size_key 必须为 number")
        index = next(i for i, item in enumerate(dataset.columns) if item.key == series.size_key)
        for value in (row[index] for row in dataset.rows):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                raise ValueError("bubble size 不能为负数")
    elif series.mark in {"pie", "donut"}:
        if series.category_key is None or series.value_key is None:
            raise ValueError("pie/donut 必须设置 category_key/value_key")
    elif series.mark == "boxplot":
        if any(
            key is None
            for key in (
                series.category_key,
                series.min_key,
                series.q1_key,
                series.median_key,
                series.q3_key,
                series.max_key,
            )
        ):
            raise ValueError("boxplot 必须设置五数概括字段")
    elif series.mark == "heatmap":
        if series.x_key is None or series.y_key is None or series.value_key is None:
            raise ValueError("heatmap 必须设置 x_key/y_key/value_key")
    numeric_keys = [
        key
        for key in (
            series.value_key,
            series.size_key,
            series.min_key,
            series.q1_key,
            series.median_key,
            series.q3_key,
            series.max_key,
        )
        if key
    ]
    if series.mark in {"line", "area", "bar", "scatter", "bubble"} and series.y_key:
        numeric_keys.append(series.y_key)
    if any(dataset.column_type(key) != "number" for key in numeric_keys):
        raise ValueError("series 数值字段必须引用 number 列")
    if series.mark in {"scatter", "bubble"} and dataset.column_type(series.x_key or "") != "number":
        raise ValueError("scatter/bubble x_key 必须引用 number 列")
    if series.mark in {"pie", "donut"} and dataset.column_type(series.category_key or "") not in {
        "string",
        "datetime",
    }:
        raise ValueError("pie/donut category_key 必须引用分类列")
    if series.mark == "boxplot" and dataset.column_type(series.category_key or "") != "string":
        raise ValueError("boxplot category_key 必须引用 string 列")
    if series.x_axis_id is not None and axes.get(series.x_axis_id, None) is None:
        raise ValueError("series x_axis_id 不存在")
    if series.y_axis_id is not None and axes.get(series.y_axis_id, None) is None:
        raise ValueError("series y_axis_id 不存在")


class PresentationArtifactRefV2(_StrictModel):
    artifact_id: str = Field(pattern=r"^chart_[a-f0-9]{24}$")
    kind: Literal["chart"] = "chart"
    schema_version: Literal[2] = 2
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    session_id: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{24}$")
    created_at: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)


class ChartArtifactV2(PresentationArtifactRefV2):
    spec: ChartSpecV2

    @model_validator(mode="after")
    def _integrity_matches_payload(self) -> ChartArtifactV2:
        expected = (
            "sha256:"
            + hashlib.sha256(canonical_json_bytes(self.spec.model_dump(mode="json"))).hexdigest()
        )
        if self.content_hash != expected:
            raise ValueError("Artifact content_hash 与 spec 不一致")
        if self.size_bytes != len(canonical_json_bytes(self.model_dump(mode="json"))):
            raise ValueError("Artifact size_bytes 与载荷不一致")
        if self.run_id is not None and self.message_id != stable_message_id(self.run_id):
            raise ValueError("Artifact message_id 与 run_id 不一致")
        return self

    @property
    def ref(self) -> PresentationArtifactRefV2:
        return PresentationArtifactRefV2.model_validate(
            self.model_dump(exclude={"spec"}), strict=True
        )


def build_chart_artifact_v2(
    spec: ChartSpecV2, *, session_id: str, run_id: str, call_id: str, created_at: str | None = None
) -> ChartArtifactV2:
    spec_data = spec.model_dump(mode="json")
    content_hash = "sha256:" + hashlib.sha256(canonical_json_bytes(spec_data)).hexdigest()
    identity = canonical_json_bytes([session_id, run_id, call_id, content_hash])
    base = {
        "artifact_id": "chart_" + hashlib.sha256(identity).hexdigest()[:24],
        "kind": "chart",
        "schema_version": 2,
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
    return ChartArtifactV2.model_validate({**base, "size_bytes": size}, strict=True)
