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
