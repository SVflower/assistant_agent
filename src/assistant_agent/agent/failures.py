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


def budget_failure(resource: BudgetResource, used: int, limit: int) -> RunFailure:
    """构造预算停止失败；文本来自白名单，不拼接外部数据。"""
    definitions: dict[BudgetResource, tuple[FailureCode, str]] = {
        "iterations": ("iteration_limit_reached", "任务已达到迭代上限。"),
        "tool_calls": ("tool_call_budget_exhausted", "任务工具调用预算已耗尽。"),
        "tool_output": ("tool_output_budget_exhausted", "任务工具输出预算已耗尽。"),
        "context": ("context_limit_exceeded", "任务上下文超过模型可用窗口。"),
    }
    code, message = definitions[resource]
    return RunFailure(
        code=code,
        safe_message=message,
        retryable=resource != "context",
        allowed_actions=("retry_run", "adjust_configuration", "start_new_run"),
        resource=resource,
        used=used,
        limit=limit,
        phase="preparing_context" if resource == "context" else "waiting_interaction",
        terminal_status="failed",
    )


def tool_failure(code: str, *, retryable: bool) -> RunFailure:
    """把工具内部 code 映射为公开且脱敏的失败分类。"""
    if code in {"permission_denied", "permission_check_failed"}:
        return RunFailure(
            code="permission_denied",
            safe_message="工具操作未获授权。",
            retryable=False,
            allowed_actions=("stop",),
            phase="executing_tool",
        )
    if code in {"mcp_transport_error", "mcp_contract_error"}:
        return RunFailure(
            code="dependency_unavailable",
            safe_message="外部工具依赖当前不可用。",
            retryable=retryable,
            allowed_actions=("retry_run", "inspect_dependency", "stop"),
            phase="executing_tool",
        )
    unknown = code == "mcp_outcome_unknown"
    return RunFailure(
        code="tool_failed",
        safe_message=("工具执行结果未知，需要人工决定恢复方式。" if unknown else "工具执行失败。"),
        retryable=retryable and not unknown,
        allowed_actions=(("resolve_uncertain_tool", "stop") if unknown else ("retry_run", "stop")),
        phase="executing_tool",
        unknown_side_effect=unknown,
        terminal_status="paused" if unknown else None,
    )


def provider_failure(code: str, safe_message: str, retryable: bool) -> RunFailure:
    """将 llm 层已脱敏的分类提升为公共 Run failure。"""
    provider_codes: dict[str, FailureCode] = {
        "provider_rate_limited": "provider_rate_limited",
        "provider_unavailable": "provider_unavailable",
        "provider_timeout": "provider_timeout",
    }
    public_code = provider_codes.get(code, "internal_error")
    return RunFailure(
        code=public_code,
        safe_message=safe_message,
        retryable=retryable,
        allowed_actions=("retry_run", "stop") if retryable else ("adjust_configuration", "stop"),
        phase="calling_model",
        terminal_status="failed",
    )
