"""配置层：定义并加载 / 校验配置。"""

from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.schema import (
    AgentConfig,
    AppConfig,
    AttachmentsConfig,
    ProviderConfig,
    ToolsConfig,
    UIConfig,
)

__all__ = [
    "load_config",
    "ConfigError",
    "AppConfig",
    "ProviderConfig",
    "AgentConfig",
    "AttachmentsConfig",
    "ToolsConfig",
    "UIConfig",
]
