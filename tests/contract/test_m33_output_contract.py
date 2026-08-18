from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import assistant_agent.contracts as contracts
import assistant_agent.service as service
from assistant_agent.agent.run.state import RunState
from assistant_agent.application.models import SESSION_SCHEMA_VERSION
from assistant_agent.contracts.outputs import OutputArtifactV1
from assistant_agent.contracts.sessions import SessionSnapshot


def _artifact() -> OutputArtifactV1:
    return OutputArtifactV1(
        output_id="out_" + "a" * 32,
        session_id="session-1",
        run_id="run-1",
        message_id="msg_" + "b" * 24,
        call_id="call-1",
        filename="report.html",
        title="Report",
        media_type="text/html",
        size_bytes=12,
        content_hash="c" * 64,
        created_at="2026-08-13T01:02:03Z",
        disposition="inline",
        preview_supported=True,
    )


def test_m33_versions_and_public_exports_are_current() -> None:
    assert contracts.AGENT_SERVICE_CONTRACT_VERSION == 5
    assert service.AGENT_SERVICE_CONTRACT_VERSION == 5
    assert contracts.SESSION_CONTRACT_VERSION == 5
    assert SESSION_SCHEMA_VERSION == 5
    assert RunState.model_fields["schema_version"].default == 11
    assert contracts.OUTPUT_CONTRACT_VERSION == 1
    assert service.OutputArtifactV1 is contracts.OutputArtifactV1


def test_output_ref_is_strict_and_never_exposes_path() -> None:
    artifact = _artifact()
    assert "path" not in artifact.model_dump()
    with pytest.raises(ValidationError):
        OutputArtifactV1.model_validate({**artifact.model_dump(), "path": str(Path.cwd())})


def test_session_snapshot_v5_validates_output_ownership() -> None:
    artifact = _artifact()
    snapshot = SessionSnapshot(id="session-1", outputs=(artifact,))
    assert snapshot.schema_version == 5
    with pytest.raises(ValidationError, match="Output 归属"):
        SessionSnapshot(id="other-session", outputs=(artifact,))
