"""Session/Run 应用用例共享的持久化无关数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeGuard

from assistant_agent.contracts.charts import AssistantMessageSnapshot, ChartArtifact
from assistant_agent.contracts.sessions import PublicMessageSnapshot

SESSION_SCHEMA_VERSION = 2
EMPTY_SESSION_TITLE = "（空会话）"
_PREVIEW_LEN = 40
PublicRunStatus = Literal["running", "paused", "cancelled", "completed", "failed"]
PUBLIC_RUN_STATUSES: frozenset[str] = frozenset(
    {"running", "paused", "cancelled", "completed", "failed"}
)


def is_public_run_status(value: str) -> TypeGuard[PublicRunStatus]:
    return value in PUBLIC_RUN_STATUSES


def collapse_unicode_whitespace(value: str) -> str:
    return " ".join(value.split())


def automatic_session_title(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        text = collapse_unicode_whitespace(str(message.get("content") or ""))
        if text:
            return text[:80]
    return EMPTY_SESSION_TITLE


def public_message_count(messages: list[dict[str, Any]]) -> int:
    return sum(message.get("role") in {"user", "assistant"} for message in messages)


def public_preview(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") not in {"user", "assistant"}:
            continue
        text = collapse_unicode_whitespace(str(message.get("content") or ""))
        if text:
            return text[:_PREVIEW_LEN] + ("…" if len(text) > _PREVIEW_LEN else "")
    return EMPTY_SESSION_TITLE


@dataclass
class Session:
    id: str
    created_at: str
    updated_at: str
    schema_version: int = SESSION_SCHEMA_VERSION
    title: str = EMPTY_SESSION_TITLE
    title_source: str = "auto"
    metadata_version: int = 1
    provider: str = ""
    model: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    compaction_checkpoint: dict[str, Any] | None = None
    presentations: list[ChartArtifact] = field(default_factory=list)
    assistant_messages: list[AssistantMessageSnapshot] = field(default_factory=list)
    message_ledger: list[PublicMessageSnapshot] = field(default_factory=list)
    fork_origin: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "title": self.title,
            "title_source": self.title_source,
            "metadata_version": self.metadata_version,
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
            "message_ledger": [item.model_dump(mode="json") for item in self.message_ledger],
            "fork_origin": self.fork_origin,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=data["id"],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            schema_version=data.get("schema_version", SESSION_SCHEMA_VERSION),
            title=data.get("title", EMPTY_SESSION_TITLE),
            title_source=data.get("title_source", "auto"),
            metadata_version=data.get("metadata_version", 1),
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
            message_ledger=[
                PublicMessageSnapshot.model_validate(item, strict=True)
                for item in data.get("message_ledger", [])
            ],
            fork_origin=data.get("fork_origin"),
        )

    @property
    def preview(self) -> str:
        if self.message_ledger:
            return public_preview(
                [
                    {"role": message.role, "content": message.content}
                    for message in self.message_ledger
                ]
            )
        return public_preview(self.messages)


@dataclass
class SessionMeta:
    id: str
    title: str
    title_source: str
    metadata_version: int
    created_at: str
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
