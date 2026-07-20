"""公共服务边界的类型化异常。"""

from __future__ import annotations


class AgentServiceError(RuntimeError):
    """公共服务错误基类。"""


class RuntimeConfigError(AgentServiceError):
    pass


class RuntimeInitializationError(AgentServiceError):
    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(message)


class RuntimePolicyError(RuntimeConfigError):
    pass


class RuntimeDependencyError(RuntimeInitializationError):
    def __init__(self, dependency: str, category: str, message: str) -> None:
        self.dependency = dependency
        self.category = category
        super().__init__("dependency", message)


class RuntimeClosedError(AgentServiceError):
    code = "runtime_closed"


class SessionBusyError(AgentServiceError):
    code = "session_busy"


class SessionRunConflictError(AgentServiceError):
    code = "session_run_conflict"


class InvalidSessionQueryError(AgentServiceError):
    code = "invalid_session_query"


class InvalidSessionLimitError(AgentServiceError):
    code = "invalid_session_limit"


class InvalidSessionCursorError(AgentServiceError):
    code = "invalid_session_cursor"


class InvalidSessionMetadataError(AgentServiceError):
    code = "invalid_session_metadata"


class InvalidForkRequestError(AgentServiceError):
    code = "invalid_fork_request"


class InvalidIdempotencyKeyError(AgentServiceError):
    code = "invalid_idempotency_key"


class SessionNotFoundError(AgentServiceError):
    code = "session_not_found"


class SessionMetadataConflictError(AgentServiceError):
    code = "session_metadata_conflict"

    def __init__(self, message: str, *, current_metadata_version: int) -> None:
        self.current_metadata_version = current_metadata_version
        super().__init__(message)


class SessionUnavailableError(AgentServiceError):
    code = "session_unavailable"


class SessionMigrationRequiredError(AgentServiceError):
    code = "session_migration_required"


class UserMessageNotFoundError(AgentServiceError):
    code = "user_message_not_found"


class RunNotFoundError(AgentServiceError):
    code = "run_not_found"


class RunStillActiveError(AgentServiceError):
    code = "run_still_active"


class RunNotResumableError(AgentServiceError):
    code = "run_not_resumable"


class RunNotReconcilableError(AgentServiceError):
    code = "run_not_reconcilable"


class RunNotRetryableError(AgentServiceError):
    code = "run_not_retryable"


class RunRecoveryRequiredError(AgentServiceError):
    code = "run_recovery_required"


class IdempotencyConflictError(AgentServiceError):
    code = "idempotency_conflict"


class ArtifactNotFoundError(AgentServiceError):
    code = "artifact_not_found"


class ArtifactUnavailableError(AgentServiceError):
    code = "artifact_unavailable"
