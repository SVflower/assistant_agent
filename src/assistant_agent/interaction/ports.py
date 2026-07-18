"""兼容导入；同步交互实现已迁至 application.interactions。"""

from assistant_agent.application.interactions import (
    BlockingInteractionPort,
    SafeDefaultInteractionPort,
)
from assistant_agent.contracts.interactions import InteractionPort as InteractionPort

__all__ = ["BlockingInteractionPort", "InteractionPort", "SafeDefaultInteractionPort"]
