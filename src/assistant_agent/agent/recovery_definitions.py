"""兼容导入；恢复定义检查已迁至 agent.run.definitions。"""

from assistant_agent.agent.run.recovery import DefinitionDifference, DefinitionStateMixin

__all__ = ["DefinitionDifference", "DefinitionStateMixin"]
