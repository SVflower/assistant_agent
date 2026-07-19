"""M18 公共失败契约的稳定性与脱敏测试。"""

import pytest
from pydantic import ValidationError

from assistant_agent.agent.run.failures import budget_failure, tool_failure
from assistant_agent.service import RunFailure


def test_budget_failure_exposes_machine_fields_without_sensitive_text() -> None:
    failure = budget_failure("tool_output", 30_000, 30_000)

    assert isinstance(failure, RunFailure)
    assert failure.code == "tool_output_budget_exhausted"
    assert failure.resource == "tool_output"
    assert (failure.used, failure.limit) == (30_000, 30_000)
    assert "continue" not in failure.allowed_actions


def test_unknown_tool_side_effect_requires_recovery() -> None:
    failure = tool_failure("mcp_outcome_unknown", retryable=False)

    assert failure.code == "tool_failed"
    assert failure.unknown_side_effect is True
    assert failure.terminal_status == "paused"
    assert failure.allowed_actions == ("resolve_uncertain_tool", "stop")


def test_non_terminal_tool_failure_does_not_claim_run_terminal() -> None:
    failure = tool_failure("tool_exception", retryable=True)

    assert failure.terminal_status is None


def test_failure_model_rejects_extra_sensitive_fields() -> None:
    payload = {
        "code": "internal_error",
        "safe_message": "failed",
        "phase": "calling_model",
        "raw_exception": "api_key=secret",
    }

    with pytest.raises(ValidationError, match="raw_exception"):
        RunFailure.model_validate(payload)
