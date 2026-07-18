"""可观测性兼容入口；按需加载，避免纯脱敏工具启动具体 logger。"""

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    modules = {
        "EventLogger": "assistant_agent.observability.logger",
        "NullLogger": "assistant_agent.observability.logger",
        "create_logger": "assistant_agent.observability.logger",
        "new_trace_id": "assistant_agent.observability.logger",
        "sanitize_for_display": "assistant_agent.observability.redaction",
    }
    module = modules.get(name)
    if module is not None:
        return getattr(import_module(module), name)
    raise AttributeError(name)


__all__ = [
    "EventLogger",
    "NullLogger",
    "create_logger",
    "new_trace_id",
    "sanitize_for_display",
]
