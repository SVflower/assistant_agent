"""日志与授权 UI 共用的尽力脱敏和载荷限长。"""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEY_HINTS = ("key", "token", "password", "passwd", "secret", "credential", "auth")
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})"
)
_REDACTED = "***REDACTED***"


def redact_text(value: str) -> str:
    return _SECRET_VALUE_RE.sub(_REDACTED, value)


def truncate_text(value: str, max_chars: int) -> str:
    if max_chars > 0 and len(value) > max_chars:
        return value[:max_chars] + f"…(+{len(value) - max_chars} chars)"
    return value


def _key_is_secret(key: str) -> bool:
    return any(hint in key.lower() for hint in _SECRET_KEY_HINTS)


def _sanitize_value(value: Any, max_chars: int) -> Any:
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _key_is_secret(str(key)) else _sanitize_value(item, max_chars))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, max_chars) for item in value]
    if isinstance(value, str):
        return truncate_text(redact_text(value), max_chars)
    return value


def sanitize_args(args: dict[str, Any], max_chars: int) -> dict[str, Any]:
    return {
        key: (_REDACTED if _key_is_secret(key) else _sanitize_value(value, max_chars))
        for key, value in args.items()
    }


def sanitize_for_display(value: Any, max_chars: int = 500) -> Any:
    """返回供审计或授权展示的递归脱敏副本。"""
    return _sanitize_value(value, max_chars)
