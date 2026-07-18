"""兼容导入；具体 Runtime builders 已迁至 bootstrap.tools。"""

from assistant_agent.bootstrap.tools import (
    build_permission_policy,
    discover_skills,
    register_extension_tools,
    start_mcp,
    start_web,
    start_workspace,
)
from assistant_agent.contracts.capabilities import RuntimeNotice

__all__ = [
    "RuntimeNotice",
    "build_permission_policy",
    "discover_skills",
    "register_extension_tools",
    "start_mcp",
    "start_web",
    "start_workspace",
]
