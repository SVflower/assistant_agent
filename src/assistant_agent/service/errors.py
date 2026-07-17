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


class RuntimeClosedError(AgentServiceError):
    pass


class SessionBusyError(AgentServiceError):
    pass


class SessionRunConflictError(AgentServiceError):
    pass
