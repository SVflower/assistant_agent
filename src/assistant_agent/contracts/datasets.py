"""可由普通图表和未来分析 Artifact 共同复用的中立表格数据集。"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DataType = Literal["string", "number", "datetime"]
DatasetCell = str | int | float | None


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DatasetColumnV1(_StrictModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    label: str = Field(min_length=1, max_length=128)
    data_type: DataType
    unit: str | None = Field(default=None, max_length=128)


class TabularDatasetV1(_StrictModel):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(pattern=r"^ds_[a-z0-9_]{1,48}$")
    columns: tuple[DatasetColumnV1, ...] = Field(min_length=1, max_length=12)
    rows: tuple[tuple[DatasetCell, ...], ...] = Field(default=(), max_length=5000)

    @field_validator("columns", mode="before")
    @classmethod
    def _columns_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("rows", mode="before")
    @classmethod
    def _rows_to_tuples(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return tuple(tuple(row) if isinstance(row, list) else row for row in value)

    @model_validator(mode="after")
    def _data_matches_columns(self) -> TabularDatasetV1:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("dataset column key 必须唯一")
        width = len(self.columns)
        for row in self.rows:
            if len(row) != width:
                raise ValueError("dataset 每行单元格数量必须与 columns 一致")
            for column, cell in zip(self.columns, row, strict=True):
                if cell is None:
                    continue
                if isinstance(cell, bool):
                    raise ValueError("布尔值不是合法 dataset 数字")
                if isinstance(cell, float) and not math.isfinite(cell):
                    raise ValueError("dataset 数字必须是有限值")
                if column.data_type == "number" and not isinstance(cell, (int, float)):
                    raise ValueError(f"number 列 {column.key} 只能包含数字或 null")
                if column.data_type in {"string", "datetime"} and not isinstance(cell, str):
                    raise ValueError(f"{column.data_type} 列 {column.key} 只能包含字符串或 null")
        return self

    def column_type(self, key: str) -> DataType | None:
        return next((item.data_type for item in self.columns if item.key == key), None)
