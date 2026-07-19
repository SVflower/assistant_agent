"""UI 无关的同步交互数据契约。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias

ApprovalChoice = Literal["allow", "always", "broader", "deny"]
RecoveryChoice = Literal["retry", "skip", "abort"]
BudgetResource = Literal["iterations", "tool_calls", "tool_output", "context"]


def new_request_id() -> str:
    return f"interaction-{secrets.token_hex(12)}"


@dataclass(frozen=True)
class ScopeInfo:
    capability: str
    tool: str
    target: str


@dataclass(frozen=True)
class InteractionRequestBase:
    run_id: str
    request_id: str = field(default_factory=new_request_id)
    session_id: str | None = None
    call_id: str | None = None
    expires_at: str = ""
    kind: str = "interaction"


@dataclass(frozen=True)
class ApprovalRequest(InteractionRequestBase):
    kind: Literal["approval"] = "approval"
    tool: str = ""
    capabilities: tuple[str, ...] = ()
    display_targets: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    legal_options: tuple[ApprovalChoice, ...] = ("allow", "always", "deny")
    exact_scopes: tuple[ScopeInfo, ...] = ()
    broader_scope: ScopeInfo | None = None
    broader_scope_label: str = ""


@dataclass(frozen=True)
class QuestionRequest(InteractionRequestBase):
    kind: Literal["question"] = "question"
    question: str = ""
    options: tuple[str, ...] = ()
    legal_options: tuple[Literal["answer", "unavailable"], ...] = (
        "answer",
        "unavailable",
    )


@dataclass(frozen=True)
class ContinueRequest(InteractionRequestBase):
    kind: Literal["continue"] = "continue"
    iterations_used: int = 0
    iteration_limit: int = 0
    reason: str = "iteration_limit_reached"
    resource: BudgetResource = "iterations"
    used: int = 0
    limit: int = 0
    suggested_increment: int = 0
    hard_limit: int = 0
    extension_count: int = 0
    max_extensions: int = 0
    legal_options: tuple[Literal["continue", "stop"], ...] = ("continue", "stop")

    def __post_init__(self) -> None:
        if self.resource == "iterations" and self.used == 0 and self.iterations_used:
            object.__setattr__(self, "used", self.iterations_used)
        if self.resource == "iterations" and self.limit == 0 and self.iteration_limit:
            object.__setattr__(self, "limit", self.iteration_limit)


@dataclass(frozen=True)
class DefinitionDifferenceInfo:
    field: str
    previous_hash: str
    current_hash: str


@dataclass(frozen=True)
class DefinitionChangeRequest(InteractionRequestBase):
    kind: Literal["definition_change"] = "definition_change"
    differences: tuple[DefinitionDifferenceInfo, ...] = ()
    legal_options: tuple[Literal["accept", "reject"], ...] = ("accept", "reject")


@dataclass(frozen=True)
class RecoveryRequest(InteractionRequestBase):
    kind: Literal["recovery"] = "recovery"
    tool: str = ""
    display_summary: str = ""
    duplicate_side_effect_risk: str = ""
    legal_options: tuple[RecoveryChoice, ...] = ("retry", "skip", "abort")


InteractionRequest: TypeAlias = (
    ApprovalRequest | QuestionRequest | ContinueRequest | DefinitionChangeRequest | RecoveryRequest
)


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    choice: ApprovalChoice = "deny"


@dataclass(frozen=True)
class QuestionAnswer:
    request_id: str
    answer: str = ""
    available: bool = False


@dataclass(frozen=True)
class ContinueDecision:
    request_id: str
    continue_run: bool = False


@dataclass(frozen=True)
class DefinitionChangeDecision:
    request_id: str
    accepted: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    request_id: str
    choice: RecoveryChoice = "abort"


InteractionDecision: TypeAlias = (
    ApprovalDecision
    | QuestionAnswer
    | ContinueDecision
    | DefinitionChangeDecision
    | RecoveryDecision
)


class InteractionPort(Protocol):
    """同步、安全默认拒绝的调用方交互边界。"""

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision: ...

    def ask_question(self, request: QuestionRequest) -> QuestionAnswer: ...

    def confirm_continue(self, request: ContinueRequest) -> ContinueDecision: ...

    def confirm_definition_change(
        self, request: DefinitionChangeRequest
    ) -> DefinitionChangeDecision: ...

    def decide_recovery(self, request: RecoveryRequest) -> RecoveryDecision: ...

    def close(self) -> None: ...
