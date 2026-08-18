"""M35-R1 历史消息与权威 Run 快照关联契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import assistant_agent.contracts as contracts
import assistant_agent.service as service
from assistant_agent.agent.run.state import RunState
from assistant_agent.contracts.events import EVENT_CONTRACT_VERSION


def test_history_association_is_additive_without_version_upgrade():
    assert contracts.AGENT_SERVICE_CONTRACT_VERSION == 5
    assert contracts.SESSION_CONTRACT_VERSION == 5
    assert RunState.model_fields["schema_version"].default == 11
    assert EVENT_CONTRACT_VERSION == 1
    assert contracts.ExecutionModelSnapshot is service.ExecutionModelSnapshot


def test_public_message_run_id_is_nullable_for_unproven_history():
    message = contracts.PublicMessageSnapshot(
        id="msg_111111111111111111111111",
        role="user",
        content=contracts.UserMessageInputV1.from_text("legacy").content,
    )

    assert message.run_id is None


def test_execution_model_snapshot_is_strict_and_contains_only_safe_identity():
    snapshot = contracts.ExecutionModelSnapshot(provider="cloud", model="openai/model")

    assert snapshot.model_dump() == {"provider": "cloud", "model": "openai/model"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        contracts.ExecutionModelSnapshot.model_validate(
            {
                "provider": "cloud",
                "model": "openai/model",
                "api_key": "secret",
                "base_url": "https://provider.invalid",
            }
        )


def test_history_contract_exposes_only_the_frozen_additive_fields():
    assert "run_id" in contracts.PublicMessageSnapshot.model_fields
    assert tuple(contracts.ExecutionModelSnapshot.model_fields) == ("provider", "model")
    assert {"created_at", "execution_model"} <= set(contracts.RunSnapshot.model_fields)
