"""兼容入口；ArtifactStore 已迁至 persistence。"""

from assistant_agent.persistence.artifacts import ArtifactStore

__all__ = ["ArtifactStore"]
