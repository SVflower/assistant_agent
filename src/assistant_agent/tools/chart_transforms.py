"""ChartSpecV2 使用的确定性数据变换；模型只提交原始数据和受控参数。"""

from __future__ import annotations

import math
from collections import OrderedDict
from typing import Literal, cast

from assistant_agent.contracts.charts_v2 import DerivationTraceV1
from assistant_agent.contracts.datasets import DatasetCell, DatasetColumnV1, TabularDatasetV1

Aggregate = Literal["count", "sum", "mean", "min", "max"]


class DuplicateCoordinateError(ValueError):
    """聚合语义不明确时，向模型输入边界交付有界重复事实。"""

    def __init__(self, coordinate: tuple[str, ...], count: int) -> None:
        super().__init__("重复分类/坐标必须显式设置 aggregate")
        self.coordinate = coordinate
        self.count = count


def histogram_dataset(
    source: TabularDatasetV1,
    value_key: str,
    *,
    bin_count: int | None,
    output_id: str,
) -> tuple[TabularDatasetV1, DerivationTraceV1]:
    values = sorted(_numeric_values(source, value_key))
    if not values:
        raise ValueError("histogram 原始数值不能为空")
    count, algorithm = _histogram_bin_count(values, bin_count)
    minimum, maximum = values[0], values[-1]
    if minimum == maximum:
        edges = [minimum - 0.5, maximum + 0.5]
        count = 1
    else:
        width = (maximum - minimum) / count
        edges = [minimum + width * index for index in range(count)] + [maximum]
    counts = [0] * count
    for value in values:
        index = (
            count - 1
            if value == maximum
            else min(int((value - minimum) / (edges[-1] - minimum) * count), count - 1)
        )
        counts[index] += 1
    rows: list[list[DatasetCell]] = [
        [
            f"[{_number(left)}, {_number(right)}{']' if index == count - 1 else ')'}",
            left,
            right,
            amount,
        ]
        for index, (left, right, amount) in enumerate(
            zip(edges[:-1], edges[1:], counts, strict=True)
        )
    ]
    dataset = TabularDatasetV1(
        dataset_id=output_id,
        columns=(
            DatasetColumnV1(key="bin", label="区间", data_type="string"),
            DatasetColumnV1(key="bin_start", label="下界", data_type="number"),
            DatasetColumnV1(key="bin_end", label="上界", data_type="number"),
            DatasetColumnV1(key="count", label="频数", data_type="number"),
        ),
        rows=tuple(tuple(row) for row in rows),
    )
    trace = DerivationTraceV1(
        kind="histogram",
        algorithm=algorithm,
        source_dataset_id=source.dataset_id,
        output_dataset_id=output_id,
        value_key=value_key,
        parameter=f"bin_count={count};interval=left_closed_last_right_closed",
    )
    return dataset, trace


def boxplot_dataset(
    source: TabularDatasetV1,
    value_key: str,
    *,
    group_key: str | None,
    output_id: str,
) -> tuple[TabularDatasetV1, TabularDatasetV1, DerivationTraceV1]:
    indexes = _column_indexes(source)
    if value_key not in indexes or source.column_type(value_key) != "number":
        raise ValueError("boxplot value_key 必须为 number 列")
    if group_key is not None and group_key not in indexes:
        raise ValueError("boxplot group_key 不存在")
    groups: OrderedDict[str, list[float]] = OrderedDict()
    for row in source.rows:
        value = row[indexes[value_key]]
        if value is None:
            continue
        group_value = "全部" if group_key is None else row[indexes[group_key]]
        if group_value is None:
            continue
        groups.setdefault(str(group_value), []).append(float(value))
    if not groups or len(groups) > 50:
        raise ValueError("boxplot 分组数量必须在 1..50")
    rows: list[list[DatasetCell]] = []
    outlier_rows: list[list[DatasetCell]] = []
    for group, raw_values in groups.items():
        values = sorted(raw_values)
        q1, median, q3 = (_type7_quantile(values, q) for q in (0.25, 0.5, 0.75))
        iqr = q3 - q1
        low_fence, high_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        whisker_low = next(value for value in values if value >= low_fence)
        whisker_high = next(value for value in reversed(values) if value <= high_fence)
        outliers = [value for value in values if value < whisker_low or value > whisker_high]
        rows.append([group, whisker_low, q1, median, q3, whisker_high, len(outliers)])
        outlier_rows.extend([[group, value] for value in outliers])
    dataset = TabularDatasetV1(
        dataset_id=output_id,
        columns=(
            DatasetColumnV1(key="group", label="分组", data_type="string"),
            DatasetColumnV1(key="min", label="下须", data_type="number"),
            DatasetColumnV1(key="q1", label="Q1", data_type="number"),
            DatasetColumnV1(key="median", label="中位数", data_type="number"),
            DatasetColumnV1(key="q3", label="Q3", data_type="number"),
            DatasetColumnV1(key="max", label="上须", data_type="number"),
            DatasetColumnV1(key="outlier_count", label="异常值数量", data_type="number"),
        ),
        rows=tuple(tuple(row) for row in rows),
    )
    outlier_dataset = TabularDatasetV1(
        dataset_id=f"{output_id}_outliers",
        columns=(
            DatasetColumnV1(key="group", label="分组", data_type="string"),
            DatasetColumnV1(key="value", label="原始异常值", data_type="number"),
        ),
        rows=tuple(tuple(row) for row in outlier_rows),
    )
    return (
        dataset,
        outlier_dataset,
        DerivationTraceV1(
            kind="boxplot",
            algorithm="type7_iqr_v1",
            source_dataset_id=source.dataset_id,
            output_dataset_id=output_id,
            value_key=value_key,
            group_key=group_key,
            parameter="quartile=type7;whisker=1.5_iqr;outliers=original_observations",
        ),
    )


def percent_dataset(
    source: TabularDatasetV1,
    category_key: str,
    value_keys: list[str],
    *,
    output_id: str,
) -> tuple[TabularDatasetV1, DerivationTraceV1]:
    indexes = _column_indexes(source)
    if category_key not in indexes or any(
        source.column_type(key) != "number" for key in value_keys
    ):
        raise ValueError("percent stack 引用无效")
    grouped: OrderedDict[str, list[float]] = OrderedDict()
    for row in source.rows:
        category = row[indexes[category_key]]
        if category is None:
            continue
        raw_values = [row[indexes[key]] for key in value_keys]
        if any(value is None for value in raw_values):
            continue
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in raw_values
        ):
            raise ValueError("percent stack 值必须为有限数字")
        values = [float(cast(int | float, value)) for value in raw_values]
        if any(value < 0 for value in values):
            raise ValueError("percent stack 不允许负值")
        bucket = grouped.setdefault(str(category), [0.0] * len(value_keys))
        for index, value in enumerate(values):
            bucket[index] += float(value)
    rows: list[list[DatasetCell]] = []
    for category, values in grouped.items():
        total = sum(values)
        if total <= 0:
            raise ValueError("percent stack 分类总和必须大于 0")
        rows.append([category, *(value / total * 100 for value in values)])
    columns: list[DatasetColumnV1] = [
        DatasetColumnV1(key=category_key, label=category_key, data_type="string")
    ]
    columns.extend(
        DatasetColumnV1(key=key, label=key, data_type="number", unit="%") for key in value_keys
    )
    return TabularDatasetV1(
        dataset_id=output_id,
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
    ), DerivationTraceV1(
        kind="percent",
        algorithm="category_percent_v1",
        source_dataset_id=source.dataset_id,
        output_dataset_id=output_id,
        value_key=",".join(value_keys),
        group_key=category_key,
        parameter="nonnegative=true;zero_total=reject",
    )


def aggregate_dataset(
    source: TabularDatasetV1,
    group_keys: list[str],
    value_key: str,
    aggregate: Aggregate | None,
    *,
    output_id: str,
) -> tuple[TabularDatasetV1, DerivationTraceV1 | None]:
    indexes = _column_indexes(source)
    if any(key not in indexes for key in [*group_keys, value_key]):
        raise ValueError("aggregate 引用列不存在")
    grouped: OrderedDict[tuple[str, ...], list[float]] = OrderedDict()
    duplicates = False
    for row in source.rows:
        group = tuple(str(row[indexes[key]]) for key in group_keys)
        value = row[indexes[value_key]]
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("aggregate value_key 必须为 number")
        duplicates = duplicates or group in grouped
        grouped.setdefault(group, []).append(float(value))
    if duplicates and aggregate is None:
        coordinate, values = next(item for item in grouped.items() if len(item[1]) > 1)
        raise DuplicateCoordinateError(coordinate, len(values))
    operation: Aggregate = aggregate or "sum"
    rows: list[list[DatasetCell]] = [
        [*group, _aggregate(values, operation)] for group, values in grouped.items()
    ]
    columns = [DatasetColumnV1(key=key, label=key, data_type="string") for key in group_keys] + [
        DatasetColumnV1(key=value_key, label=value_key, data_type="number")
    ]
    dataset = TabularDatasetV1(
        dataset_id=output_id,
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
    )
    if not duplicates:
        return dataset, None
    trace = DerivationTraceV1(
        kind="aggregate",
        algorithm="aggregate_v1",
        source_dataset_id=source.dataset_id,
        output_dataset_id=output_id,
        value_key=value_key,
        group_key=",".join(group_keys),
        parameter=operation,
    )
    return dataset, trace


def heatmap_dataset(
    source: TabularDatasetV1,
    x_key: str,
    y_key: str,
    value_key: str,
    aggregate: Aggregate | None,
    *,
    output_id: str,
) -> tuple[TabularDatasetV1, DerivationTraceV1 | None]:
    """校验 Heatmap 可渲染数据，再执行确定性坐标聚合。"""
    indexes = _column_indexes(source)
    if any(key not in indexes for key in (x_key, y_key, value_key)):
        raise ValueError("heatmap 字段必须引用已声明列")
    if not source.rows:
        raise ValueError("heatmap rows 不能为空")
    has_value = False
    for row in source.rows:
        for key in (x_key, y_key):
            value = row[indexes[key]]
            if value is None or isinstance(value, str) and not value.strip():
                raise ValueError(f"heatmap {key} 不能为 null 或空白")
        has_value = has_value or row[indexes[value_key]] is not None
    if not has_value:
        raise ValueError("heatmap value_key 不能全部为 null")
    dataset, trace = aggregate_dataset(
        source, [x_key, y_key], value_key, aggregate, output_id=output_id
    )
    if not dataset.rows:
        raise ValueError("heatmap derived dataset 不能为空")
    return dataset, trace


def _histogram_bin_count(
    values: list[float], explicit: int | None
) -> tuple[int, Literal["explicit_bins_v1", "freedman_diaconis_v1", "sturges_v1"]]:
    if explicit is not None:
        if not 1 <= explicit <= 100:
            raise ValueError("histogram bin_count 必须在 1..100")
        return explicit, "explicit_bins_v1"
    n = len(values)
    q1, q3 = _type7_quantile(values, 0.25), _type7_quantile(values, 0.75)
    width = 2 * (q3 - q1) * n ** (-1 / 3)
    if width <= 0:
        count = math.ceil(math.log2(n)) + 1
        return max(1, min(count, 100)), "sturges_v1"
    count = math.ceil((values[-1] - values[0]) / width)
    return max(1, min(count, 100)), "freedman_diaconis_v1"


def _type7_quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile 数据不能为空")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    fraction = position - lower
    if lower + 1 >= len(values):
        return values[-1]
    return values[lower] + fraction * (values[lower + 1] - values[lower])


def _numeric_values(source: TabularDatasetV1, key: str) -> list[float]:
    indexes = _column_indexes(source)
    if source.column_type(key) != "number":
        raise ValueError("原始值列必须为 number")
    values: list[float] = []
    for row in source.rows:
        value = row[indexes[key]]
        if value is not None:
            values.append(float(cast(int | float, value)))
    return values


def _column_indexes(source: TabularDatasetV1) -> dict[str, int]:
    return {column.key: index for index, column in enumerate(source.columns)}


def _aggregate(values: list[float], operation: Aggregate) -> float:
    if operation == "count":
        return float(len(values))
    if operation == "sum":
        return sum(values)
    if operation == "mean":
        return sum(values) / len(values)
    if operation == "min":
        return min(values)
    return max(values)


def _number(value: float) -> str:
    return format(value, ".12g")
