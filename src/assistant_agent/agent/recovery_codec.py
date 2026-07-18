"""运行态对象与严格 checkpoint 模型之间的显式编解码。"""

from __future__ import annotations

from assistant_agent.agent.run_state import (
    PermissionRequestState,
    ToolBudgetState,
    ToolResultState,
)
from assistant_agent.tools.models import ToolBudget, ToolResult
from assistant_agent.tools.permissions import PermissionRequest


def encode_budget(budget: ToolBudget) -> ToolBudgetState:
    return ToolBudgetState(
        max_calls=budget.max_calls,
        max_total_output_chars=budget.max_total_output_chars,
        used_calls=budget.used_calls,
        used_output_chars=budget.used_output_chars,
    )


def decode_budget(state: ToolBudgetState) -> ToolBudget:
    return ToolBudget(
        max_calls=state.max_calls,
        max_total_output_chars=state.max_total_output_chars,
        used_calls=state.used_calls,
        used_output_chars=state.used_output_chars,
    )


def encode_request(request: PermissionRequest) -> PermissionRequestState:
    return PermissionRequestState(
        tool=request.tool,
        capability=request.capability.value,
        target=request.target,
        risk=request.risk,
        metadata=request.metadata,
    )


def encode_result(result: ToolResult) -> ToolResultState:
    return ToolResultState(
        output=result.output,
        is_error=result.is_error,
        code=result.code,
        retryable=result.retryable,
        executed=result.executed,
        budget_exhausted=result.budget_exhausted,
    )


def decode_result(result: ToolResultState) -> ToolResult:
    return ToolResult(
        output=result.output,
        is_error=result.is_error,
        code=result.code,
        retryable=result.retryable,
        executed=result.executed,
        budget_exhausted=result.budget_exhausted,
    )
