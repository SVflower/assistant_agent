"""兼容导入；上下文压缩已迁至 agent.context.compaction。"""

from assistant_agent.agent.context.compaction import (
    CompactionResult,
    Compactor,
    group_turns,
)

__all__ = ["CompactionResult", "Compactor", "group_turns"]
