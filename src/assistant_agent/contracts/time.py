"""公共时间格式的确定性 UTC 规则。"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_rfc3339() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc_timestamp(value: str) -> datetime:
    """解析历史时间；无时区值按 UTC 冻结解释，绝不读取机器本地时区。"""
    if not isinstance(value, str) or not value:
        raise ValueError("时间必须是非空字符串")
    source = value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
    parsed = datetime.fromisoformat(source)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_utc_timestamp(value: str) -> str:
    return parse_utc_timestamp(value).isoformat(timespec="seconds").replace("+00:00", "Z")
