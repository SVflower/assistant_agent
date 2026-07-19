"""Session/Run 应用用例共享的持久化无关数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assistant_agent.contracts.charts import AssistantMessageSnapshot, ChartArtifact

_PREVIEW_LEN = 40


@dataclass
class Session:
    id: str
    created_at: str
    updated_at: str
    provider: str = ""
    model: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    compaction_checkpoint: dict[str, Any] | None = None
    presentations: list[ChartArtifact] = field(default_factory=list)
    assistant_messages: list[AssistantMessageSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "model": self.model,
            "messages": self.messages,
            "compaction_checkpoint": self.compaction_checkpoint,
            "presentations": [item.model_dump(mode="json") for item in self.presentations],
            "assistant_messages": [
                item.model_dump(mode="json") for item in self.assistant_messages
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=data["id"],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            messages=data.get("messages", []),
            compaction_checkpoint=data.get("compaction_checkpoint"),
            presentations=[
                ChartArtifact.model_validate(item) for item in data.get("presentations", [])
            ],
            assistant_messages=[
                AssistantMessageSnapshot.model_validate(item)
                for item in data.get("assistant_messages", [])
            ],
        )

    @property
    def preview(self) -> str:
        for message in self.messages:
            if message.get("role") == "user":
                text = str(message.get("content") or "").strip().replace("\n", " ")
                return text[:_PREVIEW_LEN] + ("…" if len(text) > _PREVIEW_LEN else "")
        return "（空会话）"


@dataclass
class SessionMeta:
    id: str
    updated_at: str
    message_count: int
    preview: str


@dataclass(frozen=True)
class RunMeta:
    id: str
    status: str
    phase: str
    session_id: str | None
    updated_at: str
    preview: str


@dataclass(frozen=True)
class RunResumeInfo:
    run_id: str
    session_id: str | None
    provider: str
    interactive: bool
    created_at: str
    updated_at: str
