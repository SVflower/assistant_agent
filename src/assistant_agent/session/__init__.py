"""兼容入口；会话持久化实现已迁至 assistant_agent.persistence。"""

import sys
from importlib import import_module

_IMPL = import_module("assistant_agent.persistence")
for _name in ("run_store", "store"):
    _module = import_module(f"assistant_agent.persistence.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module

__all__ = _IMPL.__all__
globals().update({name: getattr(_IMPL, name) for name in __all__})
