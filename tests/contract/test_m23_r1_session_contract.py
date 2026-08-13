"""M23-R1 Session catalog 与元数据公共契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import assistant_agent.service as service
from assistant_agent import contracts
from assistant_agent.agent.run.state import RunState
from assistant_agent.contracts.events import EVENT_CONTRACT_VERSION


def test_m23_r1_public_contract_is_strict_and_exported():
    assert service.SESSION_CONTRACT_VERSION == 5
    assert contracts.SESSION_CONTRACT_VERSION == 5
    assert hasattr(service.AgentService, "get_session_summary")
    assert tuple(service.LastRunSummary.model_fields) == ("id", "status", "updated_at")
    assert tuple(service.SessionSummary.model_fields) == (
        "id",
        "title",
        "title_source",
        "metadata_version",
        "created_at",
        "updated_at",
        "message_count",
        "preview",
        "last_run",
    )
    assert tuple(service.SessionCatalogPage.model_fields) == ("items", "next_cursor")
    assert tuple(service.UpdateSessionMetadataRequest.model_fields) == (
        "title",
        "expected_metadata_version",
    )
    summary = service.SessionSummary(
        id="session-1",
        title="标题",
        title_source="auto",
        metadata_version=1,
        created_at="2026-07-20T00:00:00Z",
        updated_at="2026-07-20T00:00:00Z",
        message_count=0,
        preview="（空会话）",
        last_run=None,
    )
    page = service.SessionCatalogPage(items=[summary], next_cursor=None)
    assert page.items == (summary,)
    assert contracts.SessionSummary is service.SessionSummary
    assert contracts.UpdateSessionMetadataRequest is service.UpdateSessionMetadataRequest
    with pytest.raises(ValidationError):
        service.UpdateSessionMetadataRequest(
            title="valid",
            expected_metadata_version=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        service.UpdateSessionMetadataRequest(
            title="valid",
            expected_metadata_version=1,
            extra="forbidden",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        service.LastRunSummary(id="run-1", status="completed", updated_at="2026-07-20T00:00:00")


def test_m23_r1_stable_errors_and_existing_versions():
    assert service.InvalidSessionQueryError.code == "invalid_session_query"
    assert service.InvalidSessionLimitError.code == "invalid_session_limit"
    assert service.InvalidSessionCursorError.code == "invalid_session_cursor"
    assert service.InvalidSessionMetadataError.code == "invalid_session_metadata"
    assert service.SessionNotFoundError.code == "session_not_found"
    assert service.SessionMetadataConflictError.code == "session_metadata_conflict"
    assert service.SessionUnavailableError.code == "session_unavailable"
    assert EVENT_CONTRACT_VERSION == 1
    assert RunState.model_fields["schema_version"].default == 9
