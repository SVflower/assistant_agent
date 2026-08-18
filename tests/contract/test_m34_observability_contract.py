"""M34 Agent -> API additive 可观测契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import assistant_agent.contracts as contracts
import assistant_agent.service as service
from assistant_agent.agent.run.observability import new_observability
from assistant_agent.contracts.events import StepEvent
from assistant_agent.contracts.observability import ContextUsageSnapshot


def test_public_exports_and_versions_are_additive() -> None:
    assert contracts.OBSERVABILITY_CONTRACT_VERSION == 1
    assert service.OBSERVABILITY_CONTRACT_VERSION == 1
    assert contracts.AGENT_SERVICE_CONTRACT_VERSION == service.AGENT_SERVICE_CONTRACT_VERSION == 5
    assert contracts.EVENT_CONTRACT_VERSION == service.EVENT_CONTRACT_VERSION == 1
    assert contracts.RunObservabilitySnapshot is service.RunObservabilitySnapshot
    assert contracts.TrajectoryEntry is service.TrajectoryEntry


def test_step_event_carries_optional_snapshot_and_trajectory_upsert() -> None:
    snapshot = new_observability("run-contract", "2026-08-17T00:00:00Z")
    event = StepEvent(
        kind="activity",
        phase="calling_model",
        observability=snapshot,
        trajectory_entry=snapshot.trajectory[-1],
    )

    assert event.contract_version == 1
    assert event.observability == snapshot
    assert event.trajectory_entry is not None
    assert event.trajectory_entry.entry_id.startswith("traj_")


def test_unavailable_context_cannot_claim_a_value() -> None:
    with pytest.raises(ValidationError, match="unavailable"):
        ContextUsageSnapshot(used_tokens=10, source="unavailable")
