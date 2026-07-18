"""兼容导入；Runtime 能力 DTO 已迁至 assistant_agent.contracts。"""

from assistant_agent.contracts.capabilities import (
    MCPServerCapability,
    MCPStatus,
    RuntimeCapabilities,
    SkillCapability,
)

__all__ = ["MCPServerCapability", "MCPStatus", "RuntimeCapabilities", "SkillCapability"]
