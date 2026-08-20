"""把紧凑模型草稿归一化为严格 ChartSpecV2。"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any, Literal, cast

from assistant_agent.contracts.charts_v2 import (
    AnnotationSpecV1,
    AxisSpecV1,
    ChartLayoutV1,
    ChartPanelV1,
    ChartSpecV2,
    ChartTypeV2,
    DerivationTraceV1,
    ErrorBarSpecV1,
    ReferenceBandSpecV1,
    ReferenceLineSpecV1,
    SeriesMark,
    SeriesSpecV1,
)
from assistant_agent.contracts.datasets import DatasetColumnV1, TabularDatasetV1
from assistant_agent.tools.chart_input import ChartInputError, _infer_data_type
from assistant_agent.tools.chart_transforms import (
    DuplicateCoordinateError,
    aggregate_dataset,
    boxplot_dataset,
    heatmap_dataset,
    histogram_dataset,
    percent_dataset,
)

_FORBIDDEN_KEYS = {
    "option",
    "formatter",
    "html",
    "url",
    "graphic",
    "script",
    "function",
    "style",
    "__proto__",
    "prototype",
    "constructor",
}
_ROOT_KEYS = {
    "schema_version",
    "chart_type",
    "title",
    "description",
    "source_label",
    "demo_data",
    "columns",
    "rows",
    "x_key",
    "y_key",
    "category_key",
    "value_key",
    "group_key",
    "size_key",
    "series",
    "bin_count",
    "aggregate",
    "reference_lines",
    "reference_bands",
    "error_bars",
    "annotations",
    "panels",
    "layout",
}
_PANEL_KEYS = _ROOT_KEYS - {
    "schema_version",
    "title",
    "description",
    "source_label",
    "columns",
    "rows",
    "panels",
    "layout",
} | {"panel_title"}
_DATASET_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_DIRECT_KEY_FIELDS = {
    "x_key",
    "y_key",
    "category_key",
    "value_key",
    "group_key",
    "size_key",
}


def normalize_chart_v2_input(args: dict[str, Any]) -> ChartSpecV2:
    draft = deepcopy(args)
    supplied_version = draft.get("schema_version", 2)
    if supplied_version != 2:
        raise ChartInputError("schema_version: 只支持 ChartSpecV2（值 2）")
    draft["schema_version"] = 2
    _reject_forbidden_keys(draft)
    _reject_unknown_draft_keys(draft)
    _expand_demo_data(draft)
    _normalize_dataset_keys(draft)
    try:
        source = _source_dataset(draft)
        panel_drafts = draft.get("panels") or [draft]
        if not isinstance(panel_drafts, list) or not 1 <= len(panel_drafts) <= 4:
            raise ChartInputError("panels 必须是 1..4 个对象")
        datasets = [source]
        panels: list[ChartPanelV1] = []
        derivations: list[DerivationTraceV1] = []
        explicit_panels = bool(draft.get("panels"))
        for index, panel_draft in enumerate(panel_drafts):
            if not isinstance(panel_draft, dict):
                raise ChartInputError(f"panels[{index}] 必须是对象")
            try:
                panel, derived, traces = _build_panel(panel_draft, source, index)
            except DuplicateCoordinateError as exc:
                path = f"panels[{index}].aggregate" if explicit_panels else "aggregate"
                coordinate = [item[:80] for item in exc.coordinate]
                allowed = ["count", "sum", "mean", "min", "max"]
                raise ChartInputError(
                    f"{path}: 坐标 {coordinate!r} 重复 {exc.count} 次；"
                    f"请显式选择 {'/'.join(allowed)}，Agent 不会猜测聚合语义",
                    metadata={
                        "field_path": path,
                        "allowed_values": allowed,
                        "duplicate_coordinate": coordinate,
                        "duplicate_count": exc.count,
                    },
                ) from None
            panels.append(panel)
            datasets.extend(derived)
            derivations.extend(traces)
        return ChartSpecV2(
            title=str(draft.get("title") or "图表"),
            description=draft.get("description"),
            source_label=draft.get("source_label"),
            datasets=tuple(datasets),
            layout=ChartLayoutV1(
                columns=int((draft.get("layout") or {}).get("columns", 1)),
                panel_order=tuple(item.panel_id for item in panels),
                shared_legend=bool((draft.get("layout") or {}).get("shared_legend", True)),
            ),
            panels=tuple(panels),
            derivations=tuple(derivations),
        )
    except ChartInputError:
        raise
    except (TypeError, ValueError) as exc:
        raise ChartInputError(_safe_error(exc)) from None


def _expand_demo_data(draft: dict[str, Any]) -> None:
    demo = draft.pop("demo_data", None)
    if demo is None:
        return
    if not isinstance(demo, dict):
        raise ChartInputError("demo_data 必须是对象")
    if "columns" in draft or "rows" in draft:
        raise ChartInputError("demo_data 与 columns/rows 不能同时提供")
    if draft.get("panels"):
        raise ChartInputError("demo_data 仅支持单面板演示图表")
    chart_type = draft.get("chart_type")
    if chart_type not in {"line", "area", "scatter"}:
        raise ChartInputError("demo_data 仅支持 line/area/scatter")

    row_count = demo.get("row_count")
    pattern = demo.get("pattern")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or not 1 <= row_count <= 5000:
        raise ChartInputError("demo_data.row_count 必须是 1..5000 的整数")
    if pattern not in {"sine", "trend", "seasonal", "sawtooth"}:
        raise ChartInputError("demo_data.pattern 不受支持")

    x_label = _demo_text(demo, "x_label", "样本序号")
    y_label = _demo_text(demo, "y_label", "演示值")
    y_unit = demo.get("y_unit")
    if y_unit is not None and (not isinstance(y_unit, str) or len(y_unit) > 128):
        raise ChartInputError("demo_data.y_unit 必须是最长 128 字符的字符串或 null")

    draft["columns"] = [
        {"key": "sample_index", "label": x_label, "data_type": "number"},
        {"key": "demo_value", "label": y_label, "data_type": "number", "unit": y_unit},
    ]
    draft["rows"] = [
        [index, _demo_value(index, row_count, pattern)] for index in range(1, row_count + 1)
    ]
    draft["x_key"] = "sample_index"
    draft["y_key"] = "demo_value"
    draft["series"] = []
    draft["source_label"] = "示例数据（Agent 确定性生成，非真实查询结果）"
    draft.setdefault(
        "description",
        f"{row_count} 行 {pattern} 演示数据；用于验证图表容量，不代表真实业务数据。",
    )


def _demo_text(demo: dict[str, Any], key: str, default: str) -> str:
    value = demo.get(key, default)
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ChartInputError(f"demo_data.{key} 必须是 1..128 字符的字符串")
    return value


def _demo_value(index: int, row_count: int, pattern: str) -> float:
    progress = (index - 1) / max(row_count - 1, 1)
    angle = progress * math.tau * 10
    if pattern == "sine":
        value = 50 + 20 * math.sin(angle)
    elif pattern == "trend":
        value = 25 + 50 * progress + 3 * math.sin(angle)
    elif pattern == "seasonal":
        value = 40 + 20 * progress + 12 * math.sin(angle) + 4 * math.sin(angle * 3)
    else:
        value = 25 + 50 * ((progress * 10) % 1)
    return round(value, 6)


def _normalize_dataset_keys(draft: dict[str, Any]) -> None:
    columns = draft.get("columns")
    if not isinstance(columns, list):
        return
    originals = [column.get("key") for column in columns if isinstance(column, dict)]
    string_keys = [key for key in originals if isinstance(key, str)]
    if len(string_keys) != len(set(string_keys)):
        raise ChartInputError("columns.key 必须唯一")

    reserved = {key for key in string_keys if _DATASET_KEY_PATTERN.fullmatch(key)}
    used: set[str] = set()
    aliases: dict[str, str] = {}
    next_alias = 1
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("key"), str):
            continue
        original = column["key"]
        if _DATASET_KEY_PATTERN.fullmatch(original) and original not in used:
            alias = original
        else:
            while f"field_{next_alias}" in reserved | used:
                next_alias += 1
            alias = f"field_{next_alias}"
            next_alias += 1
        used.add(alias)
        if alias != original:
            column["key"] = alias
            column.setdefault("label", original)
            aliases[original] = alias

    if aliases:
        _rewrite_key_references(draft, aliases)


def _rewrite_key_references(draft: dict[str, Any], aliases: dict[str, str]) -> None:
    for field in _DIRECT_KEY_FIELDS:
        value = draft.get(field)
        if isinstance(value, str) and value in aliases:
            draft[field] = aliases[value]
    series = draft.get("series")
    if isinstance(series, list):
        for item in series:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if isinstance(key, str) and key in aliases:
                item.setdefault("label", key)
                item["key"] = aliases[key]
    error_bars = draft.get("error_bars")
    if isinstance(error_bars, list):
        for item in error_bars:
            if not isinstance(item, dict):
                continue
            for field in ("series_key", "lower_key", "upper_key"):
                value = item.get(field)
                if isinstance(value, str) and value in aliases:
                    item[field] = aliases[value]
    panels = draft.get("panels")
    if isinstance(panels, list):
        for panel in panels:
            if isinstance(panel, dict):
                _rewrite_key_references(panel, aliases)


def _source_dataset(draft: dict[str, Any]) -> TabularDatasetV1:
    columns = draft.get("columns")
    rows = draft.get("rows")
    if not isinstance(columns, list) or not 1 <= len(columns) <= 12:
        raise ChartInputError("columns 必须是 1..12 个对象")
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ChartInputError("rows 必须是最多 5000 行的数组")
    width = len(columns)
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != width:
            raise ChartInputError(f"rows[{index}] 单元格数量必须等于 columns 数量")
    normalized = []
    for index, raw in enumerate(columns):
        if not isinstance(raw, dict):
            raise ChartInputError(f"columns[{index}] 必须是对象")
        column = dict(raw)
        if "data_type" not in column:
            values = [row[index] for row in rows if row[index] is not None]
            column["data_type"] = _infer_data_type(values, index)
        normalized.append(DatasetColumnV1.model_validate(column, strict=True))
    try:
        return TabularDatasetV1(
            dataset_id="ds_source",
            columns=tuple(normalized),
            rows=tuple(tuple(row) for row in rows),
        )
    except ValueError as exc:
        raise ChartInputError(_safe_error(exc)) from None


def _build_panel(
    draft: dict[str, Any], source: TabularDatasetV1, index: int
) -> tuple[ChartPanelV1, list[TabularDatasetV1], list[DerivationTraceV1]]:
    chart_type = str(draft.get("chart_type") or "")
    panel_id = f"panel_p{index + 1}"
    derived: list[TabularDatasetV1] = []
    traces: list[DerivationTraceV1] = []
    dataset = source
    series: list[SeriesSpecV1]
    x_key = _optional_key(draft, "x_key")
    raw_series = draft.get("series") or []
    if chart_type == "histogram":
        value_key = _required_key(draft, "value_key")
        dataset, trace = histogram_dataset(
            source, value_key, bin_count=draft.get("bin_count"), output_id=f"ds_hist_{index + 1}"
        )
        derived.append(dataset)
        traces.append(trace)
        x_key = "bin"
        series = [_series(index, 0, "频数", "bar", dataset.dataset_id, x_key="bin", y_key="count")]
    elif chart_type == "boxplot":
        value_key = _required_key(draft, "value_key")
        group_key = _optional_key(draft, "group_key")
        dataset, outliers, trace = boxplot_dataset(
            source, value_key, group_key=group_key, output_id=f"ds_box_{index + 1}"
        )
        derived.extend((dataset, outliers))
        traces.append(trace)
        x_key = "group"
        series = [
            SeriesSpecV1(
                series_id=f"series_p{index + 1}_s1",
                label="箱线图",
                mark="boxplot",
                dataset_id=dataset.dataset_id,
                category_key="group",
                min_key="min",
                q1_key="q1",
                median_key="median",
                q3_key="q3",
                max_key="max",
                outlier_dataset_id=outliers.dataset_id,
                x_axis_id="axis_x",
                y_axis_id="axis_y",
            )
        ]
    elif chart_type == "percent_stacked_bar":
        value_keys = _series_keys(raw_series)
        if x_key is None or len(value_keys) < 2:
            raise ChartInputError("percent_stacked_bar 需要 x_key 和至少两个 series")
        dataset, trace = percent_dataset(
            source, x_key, value_keys, output_id=f"ds_percent_{index + 1}"
        )
        derived.append(dataset)
        traces.append(trace)
        series = [
            _series(
                index,
                i,
                _series_label(raw_series[i], key),
                "bar",
                dataset.dataset_id,
                x_key=x_key,
                y_key=key,
                stack_id="percent",
            )
            for i, key in enumerate(value_keys)
        ]
    elif chart_type in {"pie", "donut"}:
        category_key, value_key = (
            _required_key(draft, "category_key"),
            _required_key(draft, "value_key"),
        )
        dataset, aggregate_trace = aggregate_dataset(
            source,
            [category_key],
            value_key,
            draft.get("aggregate"),
            output_id=f"ds_sector_{index + 1}",
        )
        if aggregate_trace is not None:
            derived.append(dataset)
            traces.append(aggregate_trace)
        else:
            derived.append(dataset)
        series = [
            SeriesSpecV1(
                series_id=f"series_p{index + 1}_s1",
                label=str(draft.get("title") or chart_type),
                mark=cast(SeriesMark, chart_type),
                dataset_id=dataset.dataset_id,
                category_key=category_key,
                value_key=value_key,
            )
        ]
        x_key = None
    elif chart_type == "heatmap":
        x_key, y_key, value_key = (
            _required_key(draft, key) for key in ("x_key", "y_key", "value_key")
        )
        dataset, aggregate_trace = heatmap_dataset(
            source,
            x_key,
            y_key,
            value_key,
            draft.get("aggregate"),
            output_id=f"ds_heat_{index + 1}",
        )
        derived.append(dataset)
        if aggregate_trace is not None:
            traces.append(aggregate_trace)
        series = [
            SeriesSpecV1(
                series_id=f"series_p{index + 1}_s1",
                label=str(draft.get("title") or "热力图"),
                mark="heatmap",
                dataset_id=dataset.dataset_id,
                x_key=x_key,
                y_key=y_key,
                value_key=value_key,
                x_axis_id="axis_x",
                y_axis_id="axis_y",
            )
        ]
    elif chart_type == "bubble":
        keys = _series_keys(raw_series)
        if x_key is None or len(keys) != 1:
            raise ChartInputError("bubble 需要 x_key 和一个 series")
        series = [
            _series(
                index,
                0,
                _series_label(raw_series[0], keys[0]),
                "bubble",
                source.dataset_id,
                x_key=x_key,
                y_key=keys[0],
                size_key=_required_key(draft, "size_key"),
            )
        ]
    else:
        series = _cartesian_series(chart_type, draft, source, index)
    x_axis, y_axes = _axes(chart_type, dataset, source, draft, x_key, series)
    panel = ChartPanelV1(
        panel_id=panel_id,
        title=draft.get("panel_title"),
        chart_type=cast(ChartTypeV2, chart_type),
        x_axis=x_axis,
        y_axes=y_axes,
        series=tuple(series),
        reference_lines=tuple(_reference_lines(draft)),
        reference_bands=tuple(_reference_bands(draft)),
        error_bars=tuple(_error_bars(draft, series)),
        annotations=tuple(_annotations(draft)),
    )
    return panel, derived, traces


def _cartesian_series(
    chart_type: str, draft: dict[str, Any], source: TabularDatasetV1, panel_index: int
) -> list[SeriesSpecV1]:
    x_key = _required_key(draft, "x_key")
    raw = draft.get("series") or []
    if not raw and chart_type in {"line", "area", "bar"}:
        y_key = _required_key(draft, "y_key")
        column = _column(source, y_key)
        raw = [
            {
                "key": y_key,
                "label": column.label if column is not None else y_key,
                "mark": chart_type,
                "axis": "left",
            }
        ]
    keys = _series_keys(raw)
    minimum = (
        2 if chart_type in {"grouped_bar", "stacked_bar", "combo_bar_line", "dual_axis"} else 1
    )
    if len(keys) < minimum:
        raise ChartInputError(f"{chart_type} 至少需要 {minimum} 个 series")
    result = []
    marks = set()
    axes = set()
    for i, key in enumerate(keys):
        item = raw[i]
        default_mark = (
            "bar"
            if chart_type in {"bar", "grouped_bar", "stacked_bar"}
            or (chart_type == "combo_bar_line" and i == 0)
            else "line"
            if chart_type in {"combo_bar_line", "dual_axis"}
            else chart_type
        )
        mark = str(item.get("mark") or default_mark)
        if chart_type == "combo_bar_line" and mark not in {"bar", "line"}:
            raise ChartInputError("combo_bar_line 的 mark 只能是 bar/line")
        if chart_type == "dual_axis" and mark not in {"bar", "line", "area"}:
            raise ChartInputError("dual_axis 的 mark 只能是 bar/line/area")
        axis = str(item.get("axis") or "left")
        marks.add(mark)
        axes.add(axis)
        result.append(
            _series(
                panel_index,
                i,
                _series_label(item, key),
                mark,
                source.dataset_id,
                x_key=x_key,
                y_key=key,
                y_axis_id="axis_y2" if axis == "right" else "axis_y",
                stack_id="stack" if chart_type == "stacked_bar" else None,
            )
        )
    if chart_type == "combo_bar_line" and not {"bar", "line"} <= marks:
        raise ChartInputError("combo_bar_line 必须同时包含 bar 和 line")
    if chart_type == "dual_axis" and axes != {"left", "right"}:
        raise ChartInputError("dual_axis 必须同时使用 left/right")
    if chart_type == "bar" and len(result) != 1:
        raise ChartInputError("bar 只能包含一个 series；多个系列请使用 grouped_bar")
    return result


def _series(
    panel: int, index: int, label: str, mark: str, dataset_id: str, **kwargs: Any
) -> SeriesSpecV1:
    x_axis_id = kwargs.pop("x_axis_id", "axis_x")
    y_axis_id = kwargs.pop("y_axis_id", "axis_y")
    return SeriesSpecV1(
        series_id=f"series_p{panel + 1}_s{index + 1}",
        label=label,
        mark=cast(SeriesMark, mark),
        dataset_id=dataset_id,
        x_axis_id=x_axis_id,
        y_axis_id=y_axis_id,
        **kwargs,
    )


def _axes(
    chart_type: str,
    dataset: TabularDatasetV1,
    source: TabularDatasetV1,
    draft: dict[str, Any],
    x_key: str | None,
    series: list[SeriesSpecV1],
) -> tuple[AxisSpecV1 | None, tuple[AxisSpecV1, ...]]:
    if chart_type in {"pie", "donut"}:
        return None, ()
    if chart_type == "heatmap":
        return (
            AxisSpecV1(
                axis_id="axis_x",
                dimension="x",
                scale="category",
                position="bottom",
                title=_column_axis_title(source, x_key),
            ),
            (
                AxisSpecV1(
                    axis_id="axis_y",
                    dimension="y",
                    scale="category",
                    position="left",
                    title=_column_axis_title(source, _optional_key(draft, "y_key")),
                ),
            ),
        )
    x_type = dataset.column_type(x_key) if x_key else "string"
    x_scale = (
        "linear"
        if x_type == "number" and chart_type in {"scatter", "bubble"}
        else ("time" if x_type == "datetime" else "category")
    )
    x_axis = AxisSpecV1(
        axis_id="axis_x",
        dimension="x",
        scale=cast(Literal["category", "linear", "time"], x_scale),
        position="bottom",
        title=_x_axis_title(chart_type, source, dataset, draft, x_key),
    )
    y_axes = [
        AxisSpecV1(
            axis_id="axis_y",
            dimension="y",
            scale="linear",
            position="left",
            title=_y_axis_title(chart_type, source, dataset, draft, series, "axis_y"),
        )
    ]
    if any(item.y_axis_id == "axis_y2" for item in series):
        y_axes.append(
            AxisSpecV1(
                axis_id="axis_y2",
                dimension="y",
                scale="linear",
                position="right",
                title=_y_axis_title(chart_type, source, dataset, draft, series, "axis_y2"),
            )
        )
    return x_axis, tuple(y_axes)


def _x_axis_title(
    chart_type: str,
    source: TabularDatasetV1,
    dataset: TabularDatasetV1,
    draft: dict[str, Any],
    x_key: str | None,
) -> str | None:
    if chart_type == "histogram":
        return _column_axis_title(source, _optional_key(draft, "value_key"))
    if chart_type == "boxplot":
        group_key = _optional_key(draft, "group_key")
        return _column_axis_title(source, group_key) if group_key else "分组"
    return _column_axis_title(source, x_key) or _column_axis_title(dataset, x_key)


def _y_axis_title(
    chart_type: str,
    source: TabularDatasetV1,
    dataset: TabularDatasetV1,
    draft: dict[str, Any],
    series: list[SeriesSpecV1],
    axis_id: str,
) -> str | None:
    if chart_type == "histogram":
        return "频数"
    if chart_type == "boxplot":
        return _column_axis_title(source, _optional_key(draft, "value_key"))
    if chart_type == "percent_stacked_bar":
        return "占比（%）"
    columns: list[DatasetColumnV1] = []
    for item in series:
        if item.y_axis_id != axis_id or item.y_key is None:
            continue
        column = _column(source, item.y_key) or _column(dataset, item.y_key)
        if column is not None and column not in columns:
            columns.append(column)
    return _columns_axis_title(columns)


def _column(dataset: TabularDatasetV1, key: str | None) -> DatasetColumnV1 | None:
    if key is None:
        return None
    return next((item for item in dataset.columns if item.key == key), None)


def _column_axis_title(dataset: TabularDatasetV1, key: str | None) -> str | None:
    column = _column(dataset, key)
    return _format_axis_title(column.label, column.unit) if column is not None else None


def _columns_axis_title(columns: list[DatasetColumnV1]) -> str | None:
    if not columns:
        return None
    units = {item.unit.strip() for item in columns if item.unit and item.unit.strip()}
    labels = list(dict.fromkeys(item.label.strip() for item in columns if item.label.strip()))
    if len(units) <= 1:
        return _format_axis_title(" / ".join(labels), next(iter(units), None))
    return _truncate_axis_title(
        " / ".join(_format_axis_title(item.label, item.unit) for item in columns)
    )


def _format_axis_title(label: str, unit: str | None) -> str:
    clean_label = label.strip()
    clean_unit = unit.strip() if unit else ""
    if not clean_unit:
        return _truncate_axis_title(clean_label)
    suffix = f"（{clean_unit}）"
    if len(suffix) >= 128:
        return suffix[:127] + "）"
    return f"{clean_label[: 128 - len(suffix)]}{suffix}"


def _truncate_axis_title(value: str) -> str:
    return value[:128]


def _reference_lines(draft: dict[str, Any]) -> list[ReferenceLineSpecV1]:
    return [
        ReferenceLineSpecV1(
            axis_id=_axis_id(item.get("axis")),
            value=item.get("value"),
            label=str(item.get("label") or "参考线"),
        )
        for item in draft.get("reference_lines") or []
    ]


def _reference_bands(draft: dict[str, Any]) -> list[ReferenceBandSpecV1]:
    return [
        ReferenceBandSpecV1(
            axis_id=_axis_id(item.get("axis")),
            start=item.get("start"),
            end=item.get("end"),
            label=str(item.get("label") or "参考区间"),
        )
        for item in draft.get("reference_bands") or []
    ]


def _error_bars(draft: dict[str, Any], series: list[SeriesSpecV1]) -> list[ErrorBarSpecV1]:
    by_key = {item.y_key: item.series_id for item in series}
    return [
        ErrorBarSpecV1(
            series_id=by_key.get(item.get("series_key"), ""),
            lower_key=str(item.get("lower_key") or ""),
            upper_key=str(item.get("upper_key") or ""),
        )
        for item in draft.get("error_bars") or []
    ]


def _annotations(draft: dict[str, Any]) -> list[AnnotationSpecV1]:
    return [
        AnnotationSpecV1(
            text=str(item.get("text") or ""),
            x_value=item.get("x_value"),
            y_value=item.get("y_value"),
        )
        for item in draft.get("annotations") or []
    ]


def _axis_id(value: Any) -> str:
    return {"x": "axis_x", "right": "axis_y2"}.get(str(value), "axis_y")


def _series_keys(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ChartInputError("series 必须是数组")
    keys = [str(item.get("key") or "") for item in raw if isinstance(item, dict)]
    if len(keys) != len(raw) or any(not key for key in keys):
        raise ChartInputError("series.key 不能为空")
    return keys


def _series_label(item: Any, key: str) -> str:
    return str(item.get("label") or key) if isinstance(item, dict) else key


def _required_key(draft: dict[str, Any], name: str) -> str:
    value = draft.get(name)
    if not isinstance(value, str) or not value:
        raise ChartInputError(f"{name} 不能为空")
    return value


def _optional_key(draft: dict[str, Any], name: str) -> str | None:
    value = draft.get(name)
    return value if isinstance(value, str) and value else None


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise ChartInputError("图表草稿包含禁止的可执行或渲染字段")
            _reject_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_keys(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ChartInputError("图表数字必须是有限值")


def _reject_unknown_draft_keys(draft: dict[str, Any]) -> None:
    _require_keys(draft, _ROOT_KEYS, "$")
    demo_data = draft.get("demo_data")
    if demo_data is not None:
        if not isinstance(demo_data, dict):
            raise ChartInputError("demo_data 必须是对象")
        _require_keys(
            demo_data,
            {"row_count", "pattern", "x_label", "y_label", "y_unit"},
            "demo_data",
        )
    _require_object_list(draft.get("columns"), {"key", "label", "data_type", "unit"}, "columns")
    _require_object_list(draft.get("series"), {"key", "label", "mark", "axis"}, "series")
    _validate_panel_children(draft, "$")
    panels = draft.get("panels")
    if panels is not None:
        if not isinstance(panels, list):
            raise ChartInputError("panels 必须是数组")
        for index, panel in enumerate(panels):
            if not isinstance(panel, dict):
                raise ChartInputError(f"panels[{index}] 必须是对象")
            _require_keys(panel, _PANEL_KEYS, f"panels[{index}]")
            _validate_panel_children(panel, f"panels[{index}]")
    layout = draft.get("layout")
    if layout is not None:
        if not isinstance(layout, dict):
            raise ChartInputError("layout 必须是对象")
        _require_keys(layout, {"columns", "shared_legend"}, "layout")


def _validate_panel_children(draft: dict[str, Any], path: str) -> None:
    _require_object_list(draft.get("series"), {"key", "label", "mark", "axis"}, f"{path}.series")
    _require_object_list(
        draft.get("reference_lines"), {"axis", "value", "label"}, f"{path}.reference_lines"
    )
    _require_object_list(
        draft.get("reference_bands"),
        {"axis", "start", "end", "label"},
        f"{path}.reference_bands",
    )
    _require_object_list(
        draft.get("error_bars"),
        {"series_key", "lower_key", "upper_key"},
        f"{path}.error_bars",
    )
    _require_object_list(
        draft.get("annotations"), {"text", "x_value", "y_value"}, f"{path}.annotations"
    )


def _require_object_list(value: Any, allowed: set[str], path: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ChartInputError(f"{path} 必须是数组")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ChartInputError(f"{path}[{index}] 必须是对象")
        _require_keys(item, allowed, f"{path}[{index}]")


def _require_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ChartInputError(f"{path} 包含未支持字段")


def _safe_error(exc: BaseException) -> str:
    errors: list[dict[str, Any]] = getattr(exc, "errors", lambda: [])()
    if not errors:
        return str(exc)[:240]
    first = errors[0]
    path = ".".join(str(item) for item in first.get("loc", ())) or "$"
    return f"{path}: {str(first.get('msg', '字段无效')).removeprefix('Value error, ')}"
