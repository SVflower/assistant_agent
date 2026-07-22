"""模型图表草稿归一化共享的安全校验。"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[Tt ].+)?$")


class ChartInputError(ValueError):
    """可安全返回给模型的短小字段错误。"""

    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.metadata = metadata or {}


def _infer_data_type(values: list[Any], column_index: int) -> str:
    field = f"columns[{column_index}].data_type"
    if not values:
        raise ChartInputError(f"{field} 缺失且全列为 null，请显式填写类型")
    if any(isinstance(value, bool) for value in values):
        raise ChartInputError(f"{field} 无法推断：布尔值不是合法图表数字")
    if all(isinstance(value, (int, float)) for value in values):
        if any(isinstance(value, float) and not math.isfinite(value) for value in values):
            raise ChartInputError(f"{field} 无法推断：数字必须为有限值")
        return "number"
    if not all(isinstance(value, str) for value in values):
        raise ChartInputError(f"{field} 无法推断：该列包含混合或复杂值")

    datetime_flags = [_is_iso_datetime(value) for value in values]
    if all(datetime_flags):
        return "datetime"
    if any(datetime_flags):
        raise ChartInputError(f"{field} 无法推断：日期时间与普通文本混合")
    return "string"


def _is_iso_datetime(value: str) -> bool:
    if not _ISO_DATE.fullmatch(value):
        return False
    try:
        if "T" in value.upper() or " " in value:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _compact_validation_error(exc: ValueError) -> str:
    errors: list[dict[str, Any]] = getattr(exc, "errors", lambda: [])()
    if not errors:
        return "图表字段组合无效，请核对列类型和字段引用"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "$"
    message = str(first.get("msg", "字段无效")).removeprefix("Value error, ")
    return f"{location}: {message}"
