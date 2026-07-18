"""兼容入口；宿主执行实现已迁至 assistant_agent.execution。"""

import sys
from importlib import import_module

_IMPL = import_module("assistant_agent.execution")
for _name in ("container_workspace", "control", "process", "process_windows", "workspace"):
    _module = import_module(f"assistant_agent.execution.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module

__all__ = _IMPL.__all__
globals().update({name: getattr(_IMPL, name) for name in __all__})
