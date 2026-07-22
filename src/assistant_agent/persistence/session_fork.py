"""从权威公开 ledger 构造不可变的 fork 初始 Session。"""

from __future__ import annotations

import hashlib
from typing import Any

from assistant_agent.application.models import (
    SESSION_SCHEMA_VERSION,
    Session,
    automatic_session_title,
)
from assistant_agent.contracts.charts import (
    ChartArtifactV2,
    PresentationArtifactRefV2,
    canonical_json_bytes,
    parse_chart_artifact,
)
from assistant_agent.contracts.errors import UserMessageNotFoundError
from assistant_agent.contracts.sessions import PublicMessageSnapshot


def fork_message_id(target_session_id: str, source_message_id: str) -> str:
    payload = f"fork-message:{target_session_id}:{source_message_id}".encode()
    return "msg_" + hashlib.sha256(payload).hexdigest()[:24]


def build_forked_session(
    source: Session,
    *,
    before_user_message_id: str,
    target_session_id: str,
    committed_at: str,
    key_hash: str,
    request_hash: str,
) -> Session:
    boundary = next(
        (
            index
            for index, message in enumerate(source.message_ledger)
            if message.id == before_user_message_id and message.role == "user"
        ),
        None,
    )
    if boundary is None:
        raise UserMessageNotFoundError("user message 不属于当前 Session")
    copied = source.message_ledger[:boundary]
    id_map = {message.id: fork_message_id(target_session_id, message.id) for message in copied}
    source_artifacts = {item.artifact_id: item for item in source.presentations}
    cloned_artifacts: list[ChartArtifactV2] = []
    ledger: list[PublicMessageSnapshot] = []
    for message in copied:
        new_id = id_map[message.id]
        refs: list[PresentationArtifactRefV2] = []
        for ref in message.artifacts:
            artifact = source_artifacts.get(ref.artifact_id)
            if artifact is None or artifact.content_hash != ref.content_hash:
                raise ValueError("源 Artifact 与 ledger 引用不一致")
            cloned = _clone_chart_artifact(
                artifact,
                target_session_id=target_session_id,
                target_message_id=new_id,
                committed_at=committed_at,
            )
            cloned_artifacts.append(cloned)
            refs.append(cloned.ref)
        reply_to = (
            id_map.get(message.reply_to_message_id)
            if message.reply_to_message_id is not None
            else None
        )
        ledger.append(
            PublicMessageSnapshot(
                id=new_id,
                role=message.role,
                created_at=message.created_at,
                reply_to_message_id=reply_to,
                content=message.content,
                artifacts=tuple(refs),
            )
        )
    raw_messages = [{"role": message.role, "content": message.content} for message in ledger]
    title = automatic_session_title(raw_messages)
    return Session(
        id=target_session_id,
        created_at=committed_at,
        updated_at=committed_at,
        schema_version=SESSION_SCHEMA_VERSION,
        title=title,
        title_source="auto",
        metadata_version=1,
        provider=source.provider,
        model=source.model,
        messages=raw_messages,
        compaction_checkpoint=None,
        presentations=cloned_artifacts,
        assistant_messages=[],
        message_ledger=ledger,
        fork_origin={
            "source_session_id": source.id,
            "before_user_message_id": before_user_message_id,
            "key_hash": key_hash,
            "request_hash": request_hash,
        },
    )


def _clone_chart_artifact(
    source: ChartArtifactV2,
    *,
    target_session_id: str,
    target_message_id: str,
    committed_at: str,
) -> ChartArtifactV2:
    identity = canonical_json_bytes(
        [target_session_id, target_message_id, source.artifact_id, source.content_hash]
    )
    base: dict[str, Any] = {
        "artifact_id": "chart_" + hashlib.sha256(identity).hexdigest()[:24],
        "kind": "chart",
        "schema_version": source.schema_version,
        "content_hash": source.content_hash,
        "session_id": target_session_id,
        "run_id": None,
        "message_id": target_message_id,
        "created_at": committed_at,
        "title": source.title,
        "spec": source.spec.model_dump(mode="json"),
    }
    size = 1
    for _ in range(4):
        size = len(canonical_json_bytes({**base, "size_bytes": size}))
    return parse_chart_artifact({**base, "size_bytes": size}, strict=True)
