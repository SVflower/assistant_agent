"""兼容入口；可观测性实现已迁至 assistant_agent.observability。"""

from importlib import import_module
from typing import Any

_IMPL = import_module("assistant_agent.observability")
__all__ = _IMPL.__all__


def __getattr__(name: str) -> Any:
    return getattr(_IMPL, name)
