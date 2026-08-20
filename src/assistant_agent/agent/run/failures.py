"""将内部错误分类为稳定、脱敏的公共 RunFailure。"""

from assistant_agent.contracts.failures import (
    ActivityPhase,
    AllowedAction,
    BudgetResource,
    BudgetSnapshot,
    ContinuationPrompt,
    ContinuationResult,
    FailureCode,
    FailureTerminalStatus,
    RunFailure,
)


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
    """将 provider 层已脱敏的分类提升为公共 Run failure。"""
    provider_codes: dict[str, FailureCode] = {
        "provider_rate_limited": "provider_rate_limited",
        "provider_unavailable": "provider_unavailable",
        "provider_timeout": "provider_timeout",
        "provider_empty_response": "provider_empty_response",
        "provider_output_truncated": "provider_output_truncated",
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


__all__ = [
    "ActivityPhase",
    "AllowedAction",
    "BudgetResource",
    "BudgetSnapshot",
    "ContinuationPrompt",
    "ContinuationResult",
    "FailureCode",
    "FailureTerminalStatus",
    "RunFailure",
    "budget_failure",
    "provider_failure",
    "tool_failure",
]
