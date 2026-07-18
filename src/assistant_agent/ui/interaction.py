"""终端 Console 到公共 InteractionPort 的适配器。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from assistant_agent.interaction import (
    ApprovalDecision,
    ApprovalRequest,
    ContinueDecision,
    ContinueRequest,
    DefinitionChangeDecision,
    DefinitionChangeRequest,
    QuestionAnswer,
    QuestionRequest,
    RecoveryDecision,
    RecoveryRequest,
)

if TYPE_CHECKING:
    from assistant_agent.ui.console import Console


class ConsoleInteractionAdapter:
    def __init__(self, console: Console) -> None:
        self.console = console

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        lines = ["需要授权："]
        lines.extend(
            f"- {capability}: {target}"
            for capability, target in zip(
                request.capabilities, request.display_targets, strict=True
            )
        )
        if request.risks:
            lines.append(f"风险：{'；'.join(request.risks)}")
        message = "\n".join(lines)
        if "broader" in request.legal_options:
            choice = self.console.confirm_scoped(message, request.broader_scope_label)
        else:
            choice = self.console.confirm(message)
        if choice not in request.legal_options:
            choice = "deny"
        return ApprovalDecision(request.request_id, choice)

    def ask_question(self, request: QuestionRequest) -> QuestionAnswer:
        answer = self.console.ask_question(request.question, list(request.options))
        return QuestionAnswer(request.request_id, answer=answer, available=bool(answer))

    def confirm_continue(self, request: ContinueRequest) -> ContinueDecision:
        if request.resource != "iterations":
            label = "工具调用" if request.resource == "tool_calls" else "工具输出字符"
            message = (
                f"{label}预算已达到 {request.used}/{request.limit}，"
                f"是否增加 {request.suggested_increment} 后继续？"
            )
            return ContinueDecision(
                request.request_id,
                continue_run=self.console.confirm(message) != "deny",
            )
        return ContinueDecision(
            request.request_id,
            continue_run=self.console.confirm_continue(request.iterations_used),
        )

    def confirm_definition_change(
        self, request: DefinitionChangeRequest
    ) -> DefinitionChangeDecision:
        summary = ", ".join(item.field for item in request.differences)
        accepted = self.console.confirm(f"Run 定义已变化（{summary}），确认使用当前定义继续？")
        return DefinitionChangeDecision(request.request_id, accepted=accepted != "deny")

    def decide_recovery(self, request: RecoveryRequest) -> RecoveryDecision:
        answer = self.console.ask_question(
            f"工具 {request.tool}（call_id={request.call_id}）执行结果未知。"
            f"{request.display_summary}\n{request.duplicate_side_effect_risk}",
            ["retry（可能重复副作用）", "skip（注入跳过结果）", "abort（保持暂停）"],
        )
        if answer.startswith("retry"):
            return RecoveryDecision(request.request_id, "retry")
        if answer.startswith("skip"):
            return RecoveryDecision(request.request_id, "skip")
        return RecoveryDecision(request.request_id, "abort")

    def close(self) -> None:
        return
