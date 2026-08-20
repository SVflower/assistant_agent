"""稳定公共服务、事件、交互和 checkpoint 契约快照。"""

from dataclasses import fields

from assistant_agent.agent.run.observability import new_observability
from assistant_agent.agent.run.state import RunState, ToolBudgetState
from assistant_agent.contracts.events import EVENT_CONTRACT_VERSION, StepEvent
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
        "AGENT_SERVICE_CONTRACT_VERSION",
        "ATTACHMENT_CONTRACT_VERSION",
        "CONTENT_PARTS_VERSION",
        "OUTPUT_CONTRACT_VERSION",
        "OBSERVABILITY_CONTRACT_VERSION",
        "MAX_TRAJECTORY_ENTRIES",
        "AgentRuntime",
        "AgentService",
        "AgentServiceError",
        "ArtifactNotFoundError",
        "ArtifactUnavailableError",
        "AttachmentContextTooLargeError",
        "AttachmentInvalidError",
        "AttachmentNotFoundError",
        "AttachmentPartV1",
        "AttachmentPayloadV1",
        "AttachmentRefV1",
        "AttachmentSummaryV1",
        "AttachmentTooLargeError",
        "AttachmentUnavailableError",
        "AttachmentUploadV1",
        "AssistantMessageSnapshot",
        "ExecutionModelSnapshot",
        "ChartArtifactV2",
        "ChartSpecV2",
        "DatasetColumnV1",
        "EVENT_CONTRACT_VERSION",
        "RunExecution",
        "RetryRunExecution",
        "RunSnapshot",
        "RunObservabilitySnapshot",
        "RunStillActiveError",
        "RunNotFoundError",
        "RunNotResumableError",
        "RunNotReconcilableError",
        "RunNotRetryableError",
        "RunRecoveryRequiredError",
        "IdempotencyConflictError",
        "InvalidForkRequestError",
        "InvalidIdempotencyKeyError",
        "RuntimeClosedError",
        "RuntimeConfigError",
        "RuntimeInitializationError",
        "RuntimeDependencyError",
        "RuntimePolicyError",
        "RuntimePolicy",
        "RuntimeCapabilities",
        "MCPServerCapability",
        "ContextUsageSnapshot",
        "MetricSource",
        "ModelUsageSnapshot",
        "OrchestrationTimingSnapshot",
        "MessageContentV1",
        "OutputArtifactV1",
        "OutputConflictError",
        "OutputInvalidError",
        "OutputLimitExceededError",
        "OutputNotFoundError",
        "OutputPayload",
        "OutputUnavailableError",
        "PendingInteractionSnapshot",
        "SkillCapability",
        "RuntimeNotice",
        "RuntimeProfile",
        "SESSION_CONTRACT_VERSION",
        "SessionBusyError",
        "SessionRunConflictError",
        "SessionRuntime",
        "SessionSnapshot",
        "PresentationArtifactRefV2",
        "UnsupportedChartSchemaError",
        "UnsupportedRunStateSchemaError",
        "UnsupportedSchemaError",
        "UnsupportedSessionSchemaError",
        "PublicMessageSnapshot",
        "StepEvent",
        "ToolDisplay",
        "BudgetSnapshot",
        "RunFailure",
        "create_runtime",
        "sync_terminal_session",
        "InvalidSessionCursorError",
        "InvalidSessionLimitError",
        "InvalidSessionMetadataError",
        "InvalidSessionQueryError",
        "LastRunSummary",
        "SessionCatalogPage",
        "SessionMetadataConflictError",
        "SessionMigrationRequiredError",
        "SessionNotFoundError",
        "SessionSummary",
        "SessionUnavailableError",
        "UserMessageNotFoundError",
        "UpdateSessionMetadataRequest",
        "TabularDatasetV1",
        "TaskPlanItem",
        "TaskPlanSnapshot",
        "TextPartV1",
        "TimingSnapshot",
        "TrajectoryEntry",
        "UnsupportedInputModalityError",
        "UserMessageInputV1",
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
        "chart",
        "output",
        "observability",
        "trajectory_entry",
    ]
    event = StepEvent(kind="reasoning", text="private")
    assert event.contract_version == 1
    assert event.sensitive is True


def test_public_roots_export_canonical_contract_types():
    from assistant_agent.contracts import AgentServiceError, RuntimeCapabilities, RuntimeNotice
    from assistant_agent.interaction import ContinueRequest as InteractionContinueRequest
    from assistant_agent.service import AgentServiceError as ServiceError
    from assistant_agent.service import RuntimeCapabilities as ServiceCapabilities
    from assistant_agent.service import RuntimeNotice as ServiceNotice

    assert InteractionContinueRequest is ContinueRequest
    assert ServiceCapabilities is RuntimeCapabilities
    assert ServiceError is AgentServiceError
    assert ServiceNotice is RuntimeNotice


def test_interaction_request_field_baseline():
    expected = {
        ApprovalRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "expires_at",
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
            "expires_at",
            "kind",
            "question",
            "options",
            "legal_options",
        ),
        ContinueRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "expires_at",
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
            "expires_at",
            "kind",
            "differences",
            "legal_options",
        ),
        RecoveryRequest: (
            "run_id",
            "request_id",
            "session_id",
            "call_id",
            "expires_at",
            "kind",
            "tool",
            "display_summary",
            "duplicate_side_effect_risk",
            "legal_options",
        ),
    }
    for model, names in expected.items():
        assert tuple(field.name for field in fields(model)) == names


def test_run_state_v8_field_baseline():
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
        "baseline_messages",
        "baseline_compaction_checkpoint",
        "retry_baseline_available",
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
        "presentations",
        "outputs",
        "observability",
        "pending_output_capture",
        "permission_grants",
        "terminal_text",
        "failure",
        "retry_safety",
        "retry_of_run_id",
        "retry_idempotency_key_hash",
        "retry_request_hash",
        "reconciliation_request_hash",
        "retry_requests",
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
        observability=new_observability("run-contract", "2026-07-19T00:00:00Z"),
        created_at="2026-07-19T00:00:00",
        updated_at="2026-07-19T00:00:00",
    )
    assert state.schema_version == 12
