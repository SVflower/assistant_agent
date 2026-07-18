"""公共运行失败、活动阶段和预算快照契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

FailureCode: TypeAlias = Literal[
    "tool_output_budget_exhausted",
    "tool_call_budget_exhausted",
    "iteration_limit_reached",
    "context_limit_exceeded",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_timeout",
    "tool_failed",
    "permission_denied",
    "dependency_unavailable",
    "internal_error",
]
AllowedAction: TypeAlias = Literal[
    "continue",
    "stop",
    "resume_run",
    "retry_run",
    "start_new_run",
    "adjust_configuration",
    "inspect_dependency",
    "resolve_uncertain_tool",
]
BudgetResource: TypeAlias = Literal["iterations", "tool_calls", "tool_output", "context"]
ActivityPhase: TypeAlias = Literal[
    "preparing_context",
    "calling_model",
    "executing_tool",
    "waiting_interaction",
    "saving_checkpoint",
    "syncing_session",
]
FailureTerminalStatus: TypeAlias = Literal["failed", "paused"]


class _PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BudgetSnapshot(_PublicModel):
    """可安全向调用方展示的当前 Run 资源用量。"""

    iterations_used: int = Field(ge=0)
    iterations_limit: int = Field(ge=1)
    tool_calls_used: int = Field(ge=0)
    tool_calls_limit: int = Field(ge=1)
    tool_output_chars_used: int = Field(ge=0)
    tool_output_chars_limit: int = Field(ge=0)


class RunFailure(_PublicModel):
    """不含第三方异常和敏感参数的稳定失败事实。"""

    code: FailureCode
    safe_message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    allowed_actions: tuple[AllowedAction, ...] = ("stop",)
    resource: BudgetResource | None = None
    used: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=0)
    terminal_status: FailureTerminalStatus | None = None
    phase: ActivityPhase
    unknown_side_effect: bool = False


@dataclass(frozen=True)
class ContinuationPrompt:
    resource: BudgetResource
    reason: str
    used: int
    limit: int
    suggested_increment: int
    hard_limit: int
    extension_count: int
    max_extensions: int


@dataclass(frozen=True)
class ContinuationResult:
    request_id: str
    continue_run: bool = False
