"""ToolContext 到公共 InteractionPort 的 DTO 转换。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant_agent.contracts.interactions import (
    ApprovalChoice,
    ApprovalRequest,
    InteractionPort,
    QuestionRequest,
    ScopeInfo,
)
from assistant_agent.tools.permissions import PermissionRequest, PermissionScope


def ask_via_port(
    port: InteractionPort,
    *,
    run_id: str,
    session_id: str | None,
    call_id: str,
    question: str,
    options: list[str],
    sanitize: Callable[[Any], object],
) -> str | None:
    request = QuestionRequest(
        run_id=run_id,
        session_id=session_id,
        call_id=call_id or None,
        question=str(sanitize(question)),
        options=tuple(str(sanitize(option)) for option in options),
    )
    try:
        answer = port.ask_question(request)
    except Exception:
        return None
    if answer.request_id != request.request_id or not answer.available:
        return None
    return answer.answer


def approve_via_port(
    port: InteractionPort,
    *,
    run_id: str,
    session_id: str | None,
    call_id: str,
    asks: list[PermissionRequest],
    risks: list[str],
    broader_scope: PermissionScope | None,
    broader_scope_label: str,
    sanitize: Callable[[Any], object],
) -> ApprovalChoice:
    can_grant_broader = broader_scope is not None
    legal_options: tuple[ApprovalChoice, ...] = (
        ("allow", "always", "broader", "deny") if can_grant_broader else ("allow", "always", "deny")
    )
    request = ApprovalRequest(
        run_id=run_id,
        session_id=session_id,
        call_id=call_id or None,
        tool=asks[0].tool,
        capabilities=tuple(item.capability.value for item in asks),
        display_targets=tuple(str(sanitize(item.display_target)) for item in asks),
        risks=tuple(risks),
        legal_options=legal_options,
        exact_scopes=tuple(
            ScopeInfo(
                item.scope.capability.value,
                item.scope.tool,
                str(sanitize(item.scope.target)),
            )
            for item in asks
        ),
        broader_scope=(
            ScopeInfo(
                broader_scope.capability.value,
                broader_scope.tool,
                str(sanitize(broader_scope.target)),
            )
            if broader_scope is not None
            else None
        ),
        broader_scope_label=broader_scope_label if can_grant_broader else "",
    )
    try:
        response = port.request_approval(request)
    except Exception:
        return "deny"
    if response.request_id != request.request_id or response.choice not in legal_options:
        return "deny"
    return response.choice
