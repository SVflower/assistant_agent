"""可观测性层（observability）：结构化事件日志与工具审计。

底层基础设施（与 config/session 同级）：被 tools/agent/main 使用，
自身不依赖任何更高层。日志只落本地文件，不进 UI 主通道。
"""

from assistant_agent.obs.logger import EventLogger, NullLogger, create_logger, new_trace_id
from assistant_agent.obs.redaction import sanitize_for_display

__all__ = [
    "EventLogger",
    "NullLogger",
    "create_logger",
    "new_trace_id",
    "sanitize_for_display",
]
