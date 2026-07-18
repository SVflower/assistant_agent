"""预算 continuation 的计算、交互与 checkpoint 适配。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from assistant_agent.agent.run.state import (
    ContinuationBudgetState,
    ContinuationDecisionState,
    RunState,
    ToolBudgetState,
)
from assistant_agent.config.schema import ContinuationConfig
from assistant_agent.contracts.failures import (
    BudgetResource,
    BudgetSnapshot,
    ContinuationPrompt,
    ContinuationResult,
)
from assistant_agent.tools.models import ToolBudget

BudgetContinueCheck = Callable[[ContinuationPrompt], ContinuationResult]


class ContinuationCoordinator(Protocol):
    state: RunState

    def continuation_state(self, resource: BudgetResource) -> ContinuationBudgetState: ...

    def extend_budget(
        self,
        *,
        request_id: str,
        resource: BudgetResource,
        current_limit: int,
        new_limit: int,
        budget: ToolBudget,
    ) -> bool: ...


class LoopCursorLike(Protocol):
    iteration: int
    iteration_budget: int


class ContinuationController:
    """维护非持久化运行的计数，并委托 coordinator 保存可恢复运行。"""

    def __init__(
        self,
        config: ContinuationConfig,
        check: BudgetContinueCheck | None,
        legacy_iteration_check: Callable[[int], bool] | None,
    ) -> None:
        self.config = config
        self.check = check
        self.legacy_iteration_check = legacy_iteration_check
        self.local_counts: dict[BudgetResource, int] = {}
        self.reset()

    def reset(self) -> None:
        self.local_counts = {
            "iterations": 0,
            "tool_calls": 0,
            "tool_output": 0,
            "context": 0,
        }

    def request(
        self,
        resource: BudgetResource,
        *,
        used: int,
        limit: int,
        budget: ToolBudget,
        coordinator: ContinuationCoordinator | None,
    ) -> int | None:
        state = coordinator.continuation_state(resource) if coordinator is not None else None
        increment, hard_limit = (
            (state.increment, state.hard_limit) if state is not None else self._limits(resource)
        )
        extension_count = state.extension_count if state else self.local_counts[resource]
        max_extensions = state.max_extensions if state else self.config.max_extensions
        if extension_count >= max_extensions or limit >= hard_limit:
            return None
        increment = min(increment, hard_limit - limit)
        prompt = ContinuationPrompt(
            resource=resource,
            reason={
                "iterations": "iteration_limit_reached",
                "tool_calls": "tool_call_budget_exhausted",
                "tool_output": "tool_output_budget_exhausted",
            }[resource],
            used=used,
            limit=limit,
            suggested_increment=increment,
            hard_limit=hard_limit,
            extension_count=extension_count,
            max_extensions=max_extensions,
        )
        result = self._decide(prompt)
        if result is None or not result.continue_run:
            return None
        new_limit = limit + increment
        if coordinator is not None:
            if not coordinator.extend_budget(
                request_id=result.request_id,
                resource=resource,
                current_limit=limit,
                new_limit=new_limit,
                budget=budget,
            ):
                return None
        else:
            self.local_counts[resource] += 1
            if resource == "tool_calls":
                budget.max_calls = new_limit
            elif resource == "tool_output":
                budget.max_total_output_chars = new_limit
        return new_limit

    def _decide(self, prompt: ContinuationPrompt) -> ContinuationResult | None:
        try:
            if self.check is not None:
                return self.check(prompt)
            if prompt.resource == "iterations" and self.legacy_iteration_check is not None:
                return ContinuationResult(
                    f"legacy-iteration-{prompt.extension_count + 1}",
                    self.legacy_iteration_check(prompt.used),
                )
        except Exception:
            return None
        return None

    def _limits(self, resource: BudgetResource) -> tuple[int, int]:
        if resource == "iterations":
            return self.config.iteration_increment, self.config.max_iterations_hard
        if resource == "tool_calls":
            return self.config.tool_call_increment, self.config.max_tool_calls_hard
        return self.config.tool_output_increment, self.config.max_tool_output_chars_hard


class ContinuationStateMixin:
    """RunCoordinator 的 continuation 状态转换；checkpoint 仍由 coordinator 实现。"""

    state: RunState

    def checkpoint(self) -> None:
        raise NotImplementedError

    def _capture_bound_context(self) -> None:
        raise NotImplementedError

    def continuation_state(self, resource: BudgetResource) -> ContinuationBudgetState:
        if resource == "iterations":
            return self.state.iteration_continuation
        if resource == "tool_calls":
            return self.state.tool_call_continuation
        if resource == "tool_output":
            return self.state.tool_output_continuation
        raise ValueError(f"不支持 continuation resource：{resource}")

    def extend_budget(
        self,
        *,
        request_id: str,
        resource: BudgetResource,
        current_limit: int,
        new_limit: int,
        budget: ToolBudget,
    ) -> bool:
        existing = next(
            (item for item in self.state.continuation_decisions if item.request_id == request_id),
            None,
        )
        if existing is not None:
            return existing.continued
        state = self.continuation_state(resource)
        allowed = (
            state.extension_count < state.max_extensions
            and new_limit > current_limit
            and new_limit <= state.hard_limit
        )
        if allowed:
            state.extension_count += 1
            if resource == "iterations":
                self.state.iteration_budget = new_limit
            elif resource == "tool_calls":
                budget.max_calls = new_limit
            else:
                budget.max_total_output_chars = new_limit
            self.state.tool_budget = ToolBudgetState(
                max_calls=budget.max_calls,
                max_total_output_chars=budget.max_total_output_chars,
                used_calls=budget.used_calls,
                used_output_chars=budget.used_output_chars,
            )
        self.state.continuation_decisions.append(
            ContinuationDecisionState(
                request_id=request_id,
                resource=resource,
                old_limit=current_limit,
                new_limit=new_limit if allowed else current_limit,
                continued=allowed,
            )
        )
        self._capture_bound_context()
        self.checkpoint()
        return allowed


def budget_snapshot(cursor: LoopCursorLike, budget: ToolBudget) -> BudgetSnapshot:
    return BudgetSnapshot(
        iterations_used=cursor.iteration,
        iterations_limit=cursor.iteration_budget,
        tool_calls_used=budget.used_calls,
        tool_calls_limit=budget.max_calls,
        tool_output_chars_used=budget.used_output_chars,
        tool_output_chars_limit=budget.max_total_output_chars,
    )
