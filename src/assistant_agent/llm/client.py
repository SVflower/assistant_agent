"""兼容导入；模型端口与 LiteLLM adapter 已迁至 providers。"""

from assistant_agent.providers.litellm import (
    LLMClient,
    classify_provider_exception,
)
from assistant_agent.providers.litellm import (
    _bypass_proxy_for_local as _bypass_proxy_for_local,
)
from assistant_agent.providers.litellm import (
    _finalize_tool_calls as _finalize_tool_calls,
)
from assistant_agent.providers.litellm import (
    _normalize_usage as _normalize_usage,
)
from assistant_agent.providers.litellm import (
    _parse_arguments as _parse_arguments,
)
from assistant_agent.providers.ports import (
    LLMError,
    ModelProviderPort,
    ProviderFailure,
    ProviderFailureCode,
    StreamEvent,
    StreamEventKind,
    ToolCall,
)

__all__ = [
    "LLMClient",
    "LLMError",
    "ModelProviderPort",
    "ProviderFailure",
    "ProviderFailureCode",
    "StreamEvent",
    "StreamEventKind",
    "ToolCall",
    "classify_provider_exception",
]
