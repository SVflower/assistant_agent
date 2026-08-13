from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from assistant_agent.config.schema import OutputConfig
from assistant_agent.contracts.outputs import (
    OutputConflictError,
    OutputInvalidError,
    OutputLimitExceededError,
    OutputUnavailableError,
)
from assistant_agent.persistence.outputs import OutputStore
from assistant_agent.tools.outputs import CreateOutputTool


def _store(tmp_path: Path, **updates: object) -> OutputStore:
    return OutputStore(tmp_path, OutputConfig(**updates))


def _publish(store: OutputStore, **updates: object):
    values = {
        "session_id": "session-1",
        "run_id": "run-1",
        "call_id": "call-1",
        "filename": "report.html",
        "media_type": "text/html",
        "content": "<h1>ok</h1>",
        "message_id": "msg_" + "a" * 24,
    }
    values.update(updates)
    return store.publish_text(**values)


def test_store_uses_date_session_layout_and_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = _publish(store)
    path = store.local_path("session-1", artifact.output_id)
    assert path.relative_to(tmp_path / "outputs").parts[-2:] == (
        "session-1",
        f"{artifact.output_id}--report.html",
    )
    assert store.get_payload("session-1", artifact.output_id).content == "<h1>ok</h1>"
    assert artifact.preview_supported is True


def test_store_rejects_escape_unknown_mime_and_limits(tmp_path: Path) -> None:
    store = _store(tmp_path, max_file_bytes=1024, max_run_bytes=1024, max_session_bytes=1024)
    with pytest.raises(OutputInvalidError):
        _publish(store, filename="../escape.html")
    with pytest.raises(OutputInvalidError):
        _publish(store, media_type="application/pdf")
    with pytest.raises(OutputLimitExceededError):
        _publish(store, content="x" * 1025)


def test_store_is_idempotent_and_detects_conflict_or_corruption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _publish(store)
    assert _publish(store) == first
    with pytest.raises(OutputConflictError):
        _publish(store, content="different")
    store.local_path("session-1", first.output_id).write_text("tampered", encoding="utf-8")
    with pytest.raises(OutputUnavailableError):
        store.get_payload("session-1", first.output_id)


def test_delete_session_isolated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _publish(store)
    other = _publish(store, session_id="session-2", run_id="run-2", call_id="call-2")
    store.delete_session("session-1")
    assert store.list("session-1") == []
    assert store.get("session-2", other.output_id) == other
    assert first.output_id != other.output_id


@dataclass
class _Context:
    output_store: OutputStore
    current_session_id: str = "session-1"
    current_run_id: str = "run-1"
    current_call_id: str = "call-1"


def test_create_output_tool_is_safe_idempotent(tmp_path: Path) -> None:
    tool = CreateOutputTool()
    ctx = _Context(_store(tmp_path))
    args = {"filename": "data.csv", "media_type": "text/csv", "content": "x,y\n1,2"}
    assert tool.replay_policy(args, ctx, []) == "safe_idempotent"  # type: ignore[arg-type]
    result = tool.run(args, ctx)  # type: ignore[arg-type]
    assert result.code == "output_created"
    assert result.output_artifact is not None
    assert result.output_artifact.message_id is not None
