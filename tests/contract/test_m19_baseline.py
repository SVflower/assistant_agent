"""M19 迁移前的公共字段与兼容语义快照。"""

from dataclasses import fields

from assistant_agent.agent.run_state import RunState, ToolBudgetState, migrate_run_document
from assistant_agent.contracts.events import EVENT_CONTRACT_VERSION, StepEvent
from assistant_agent.contracts.failures import RunFailure
from assistant_agent.contracts.interactions import (
    ApprovalRequest,
    ContinueRequest,
    DefinitionChangeRequest,
    QuestionRequest,
    RecoveryRequest,
)
from assistant_agent.service import RunExecution
from assistant_agent.service import __all__ as service_exports


def test_service_public_exports_baseline():
    assert set(service_exports) == {
        "AgentRuntime",
        "AgentService",
        "AgentServiceError",
        "EVENT_CONTRACT_VERSION",
        "RunExecution",
        "RuntimeClosedError",
        "RuntimeConfigError",
        "RuntimeInitializationError",
        "RuntimeDependencyError",
        "RuntimePolicyError",
        "RuntimePolicy",
        "RuntimeCapabilities",
        "MCPServerCapability",
        "SkillCapability",
        "RuntimeNotice",
        "SessionBusyError",
        "SessionRunConflictError",
        "SessionRuntime",
        "StepEvent",
        "ToolDisplay",
        "BudgetSnapshot",
        "RunFailure",
        "create_runtime",
        "sync_terminal_session",
    }
    assert [field.name for field in fields(RunExecution)] == ["run_id", "events", "warning"]
    execution = RunExecution("run-contract", iter(()))
    assert execution.warning == ""


def test_step_event_v1_field_baseline_and_sensitive_reasoning():
    assert EVENT_CONTRACT_VERSION == 1
    assert [field.name for field in fields(StepEvent)] == [
        "kind",
        "text",
        "tool_name",
        "tool_args",
        "is_error",
        "usage",
        "call_id",
        "display",
        "result_code",
        "result_metadata",
        "contract_version",
        "sensitive",
        "terminal_status",
        "failure",
        "phase",
        "budget",
    ]
    event = StepEvent(kind="reasoning", text="private")
    assert event.contract_version == 1
    assert event.sensitive is True


def test_legacy_contract_imports_are_identity_aliases():
    from assistant_agent.agent.events import StepEvent as LegacyStepEvent
    from assistant_agent.agent.failures import RunFailure as LegacyRunFailure
    from assistant_agent.contracts import AgentServiceError, RuntimeCapabilities, RuntimeNotice
    from assistant_agent.interaction.models import ContinueRequest as LegacyContinueRequest
    from assistant_agent.service.capabilities import RuntimeCapabilities as LegacyCapabilities
    from assistant_agent.service.errors import AgentServiceError as LegacyServiceError
    from assistant_agent.service.runtime import RuntimeNotice as LegacyRuntimeNotice

    assert LegacyStepEvent is StepEvent
    assert LegacyRunFailure is RunFailure
    assert LegacyContinueRequest is ContinueRequest
    assert LegacyCapabilities is RuntimeCapabilities
    assert LegacyServiceError is AgentServiceError
    assert LegacyRuntimeNotice is RuntimeNotice


def test_interaction_request_field_baseline():
    expected = {
        ApprovalRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "kind",
            "tool",
            "capabilities",
            "display_targets",
            "risks",
            "legal_options",
            "exact_scopes",
            "broader_scope",
            "broader_scope_label",
        ),
        QuestionRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "kind",
            "question",
            "options",
        ),
        ContinueRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "kind",
            "iterations_used",
            "iteration_limit",
            "reason",
            "resource",
            "used",
            "limit",
            "suggested_increment",
            "hard_limit",
            "extension_count",
            "max_extensions",
            "legal_options",
        ),
        DefinitionChangeRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "kind",
            "differences",
            "legal_options",
        ),
        RecoveryRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "kind",
            "tool",
            "display_summary",
            "duplicate_side_effect_risk",
            "legal_options",
        ),
    }
    for model, names in expected.items():
        assert tuple(field.name for field in fields(model)) == names


def test_run_state_v3_field_and_legacy_migration_baseline():
    assert tuple(RunState.model_fields) == (
        "schema_version",
        "run_id",
        "session_id",
        "task",
        "status",
        "phase",
        "interactive",
        "provider",
        "model",
        "system_prompt_hash",
        "tool_schema_hash",
        "messages",
        "compaction_checkpoint",
        "iteration",
        "iteration_budget",
        "tool_budget",
        "iteration_continuation",
        "tool_call_continuation",
        "tool_output_continuation",
        "continuation_decisions",
        "last_signature",
        "repeat_count",
        "tool_calls",
        "permission_grants",
        "terminal_text",
        "failure",
        "session_synced",
        "created_at",
        "updated_at",
    )
    state = RunState(
        run_id="run-contract",
        task="baseline",
        interactive=True,
        provider="fake",
        model="fake/model",
        system_prompt_hash="a" * 64,
        tool_schema_hash="b" * 64,
        iteration_budget=5,
        tool_budget=ToolBudgetState(
            max_calls=10,
            max_total_output_chars=100,
            used_calls=0,
            used_output_chars=0,
        ),
        created_at="2026-07-19T00:00:00",
        updated_at="2026-07-19T00:00:00",
    )
    for version in (1, 2):
        legacy = state.model_dump(mode="python")
        legacy["schema_version"] = version
        for key in (
            "iteration_continuation",
            "tool_call_continuation",
            "tool_output_continuation",
            "continuation_decisions",
        ):
            legacy.pop(key)
        migrated = migrate_run_document(legacy)
        assert migrated["schema_version"] == 3
        assert RunState.model_validate(migrated).schema_version == 3
