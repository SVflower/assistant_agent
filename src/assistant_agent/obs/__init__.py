"""可观测性兼容入口；按需加载，避免纯脱敏工具启动具体 logger。"""

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    modules = {
        "EventLogger": "assistant_agent.obs.logger",
        "NullLogger": "assistant_agent.obs.logger",
        "create_logger": "assistant_agent.obs.logger",
        "new_trace_id": "assistant_agent.obs.logger",
        "sanitize_for_display": "assistant_agent.obs.redaction",
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
