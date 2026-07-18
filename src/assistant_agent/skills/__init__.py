"""兼容入口；Skill adapter 已迁至 assistant_agent.integrations.skills。"""

import sys
from importlib import import_module

_IMPL = import_module("assistant_agent.integrations.skills")
for _name in ("manager", "store", "tool"):
    _module = import_module(f"assistant_agent.integrations.skills.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module

__all__ = _IMPL.__all__
globals().update({name: getattr(_IMPL, name) for name in __all__})
