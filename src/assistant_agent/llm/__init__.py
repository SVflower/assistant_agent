"""兼容模型入口；新代码使用 assistant_agent.providers。"""

from assistant_agent.llm.client import (
    LLMClient,
    LLMError,
    StreamEvent,
    ToolCall,
)

__all__ = ["LLMClient", "ToolCall", "StreamEvent", "LLMError"]
