"""M22 服务边界、checkpoint 与错误码冻结契约。"""

from __future__ import annotations

import assistant_agent.service as service
from assistant_agent import contracts
from assistant_agent.agent.run.state import RunState, migrate_run_document
from assistant_agent.contracts.events import EVENT_CONTRACT_VERSION


def test_m22_public_service_exports_are_stable():
    assert EVENT_CONTRACT_VERSION == 1
    assert service.RunStillActiveError.code == "run_still_active"
    assert service.RunNotFoundError.code == "run_not_found"
    assert service.RunNotResumableError.code == "run_not_resumable"
    assert service.RunNotReconcilableError.code == "run_not_reconcilable"
    assert service.RunNotRetryableError.code == "run_not_retryable"
    assert service.RunRecoveryRequiredError.code == "run_recovery_required"
    assert service.IdempotencyConflictError.code == "idempotency_conflict"
    assert service.SessionBusyError.code == "session_busy"
    assert service.RetryRunExecution.__dataclass_fields__.keys() == {
        "original_run_id",
        "new_run_id",
        "created",
        "events",
        "warning",
    }
    assert contracts.RunStillActiveError is service.RunStillActiveError
    assert contracts.RunRecoveryRequiredError is service.RunRecoveryRequiredError


def test_v5_migration_is_fail_closed_for_retry_and_baseline():
    current = RunState(
        run_id="run-1",
        session_id="session-1",
        task="task",
        interactive=False,
        provider="provider",
        model="model",
        system_prompt_hash="a" * 64,
        tool_schema_hash="b" * 64,
        iteration_budget=1,
        tool_budget={
            "max_calls": 1,
            "max_total_output_chars": 1,
            "used_calls": 0,
            "used_output_chars": 0,
        },
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    ).model_dump(mode="python")
    current["schema_version"] = 5
    migrated = migrate_run_document(current)
    assert migrated["schema_version"] == 7
    assert migrated["retry_safety"] == "unknown"
    assert migrated["retry_baseline_available"] is False
