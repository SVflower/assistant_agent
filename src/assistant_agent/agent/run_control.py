"""兼容导入；Run 控制映射已迁至 agent.run.control。"""

from assistant_agent.agent.run.control import finish_control

__all__ = ["finish_control"]
