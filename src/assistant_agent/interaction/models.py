"""UI 无关的同步交互数据契约。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

ApprovalChoice = Literal["allow", "always", "broader", "deny"]
RecoveryChoice = Literal["retry", "skip", "abort"]


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


@dataclass(frozen=True)
class ContinueRequest(InteractionRequestBase):
    kind: Literal["continue"] = "continue"
    iterations_used: int = 0
    iteration_limit: int = 0
    legal_options: tuple[Literal["continue", "stop"], ...] = ("continue", "stop")


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
