"""兼容导入；RunCoordinator 已迁至 agent.run.coordinator。"""

from assistant_agent.agent.run.coordinator import RecoveryChoice, RunCoordinator

__all__ = ["RecoveryChoice", "RunCoordinator"]
