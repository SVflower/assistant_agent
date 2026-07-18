"""同步 InteractionPort 及安全默认、线程阻塞实现。"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

from assistant_agent.contracts.interactions import (
    ApprovalDecision,
    ApprovalRequest,
    ContinueDecision,
    ContinueRequest,
    DefinitionChangeDecision,
    DefinitionChangeRequest,
    InteractionDecision,
    InteractionRequest,
    QuestionAnswer,
    QuestionRequest,
    RecoveryDecision,
    RecoveryRequest,
)
from assistant_agent.contracts.interactions import InteractionPort as InteractionPort


class SafeDefaultInteractionPort:
    """无人值守时的安全实现：拒绝、停止或保持暂停。"""

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(request.request_id)

    def ask_question(self, request: QuestionRequest) -> QuestionAnswer:
        return QuestionAnswer(request.request_id)

    def confirm_continue(self, request: ContinueRequest) -> ContinueDecision:
        return ContinueDecision(request.request_id)

    def confirm_definition_change(
        self, request: DefinitionChangeRequest
    ) -> DefinitionChangeDecision:
        return DefinitionChangeDecision(request.request_id)

    def decide_recovery(self, request: RecoveryRequest) -> RecoveryDecision:
        return RecoveryDecision(request.request_id)

    def close(self) -> None:
        return


@dataclass
class _Pending:
    request: InteractionRequest
    changed: threading.Event
    decision: InteractionDecision | None = None


class BlockingInteractionPort(SafeDefaultInteractionPort):
    """供服务线程有界等待、由另一线程提交结果的同步端口。"""

    def __init__(self, *, timeout: float = 60.0) -> None:
        if timeout <= 0:
            raise ValueError("interaction timeout 必须大于 0")
        self.timeout = timeout
        self._requests: queue.Queue[InteractionRequest] = queue.Queue()
        self._pending: dict[str, _Pending] = {}
        self._completed: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False

    def next_request(self, timeout: float | None = None) -> InteractionRequest | None:
        """取出下一个请求，供 API broker 发布；超时或关闭且队列空时返回 None。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
            try:
                request = self._requests.get(timeout=remaining)
            except queue.Empty:
                return None
            with self._lock:
                if (
                    request.request_id in self._pending
                    and request.request_id not in self._completed
                ):
                    return request
            if deadline is not None and time.monotonic() >= deadline:
                return None

    def respond(self, decision: InteractionDecision) -> bool:
        """提交一次合法响应。错误、过期、重复或类型不匹配均返回 False。"""
        with self._lock:
            if self._closed or decision.request_id in self._completed:
                return False
            pending = self._pending.get(decision.request_id)
            if pending is None or not _decision_matches(pending.request, decision):
                return False
            pending.decision = decision
            self._completed.add(decision.request_id)
            pending.changed.set()
            return True

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        decision = self._wait(request)
        if isinstance(decision, ApprovalDecision) and decision.choice in request.legal_options:
            return decision
        return super().request_approval(request)

    def ask_question(self, request: QuestionRequest) -> QuestionAnswer:
        decision = self._wait(request)
        if isinstance(decision, QuestionAnswer) and (
            not decision.available or decision.answer in request.options or bool(decision.answer)
        ):
            return decision
        return super().ask_question(request)

    def confirm_continue(self, request: ContinueRequest) -> ContinueDecision:
        decision = self._wait(request)
        if isinstance(decision, ContinueDecision):
            return decision
        return super().confirm_continue(request)

    def confirm_definition_change(
        self, request: DefinitionChangeRequest
    ) -> DefinitionChangeDecision:
        decision = self._wait(request)
        if isinstance(decision, DefinitionChangeDecision):
            return decision
        return super().confirm_definition_change(request)

    def decide_recovery(self, request: RecoveryRequest) -> RecoveryDecision:
        decision = self._wait(request)
        if isinstance(decision, RecoveryDecision) and decision.choice in request.legal_options:
            return decision
        return super().decide_recovery(request)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending.values())
        for item in pending:
            item.changed.set()

    def _wait(self, request: InteractionRequest) -> InteractionDecision | None:
        pending = _Pending(request, threading.Event())
        with self._lock:
            if self._closed or request.request_id in self._pending:
                return None
            self._pending[request.request_id] = pending
            self._requests.put(request)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not pending.changed.wait(remaining):
                    return None
                with self._lock:
                    if self._closed:
                        return None
                    if pending.decision is not None:
                        return pending.decision
                pending.changed.clear()
        finally:
            with self._lock:
                self._pending.pop(request.request_id, None)
                self._completed.discard(request.request_id)


def _decision_matches(request: InteractionRequest, decision: InteractionDecision) -> bool:
    pairs = (
        (ApprovalRequest, ApprovalDecision),
        (QuestionRequest, QuestionAnswer),
        (ContinueRequest, ContinueDecision),
        (DefinitionChangeRequest, DefinitionChangeDecision),
        (RecoveryRequest, RecoveryDecision),
    )
    return any(isinstance(request, left) and isinstance(decision, right) for left, right in pairs)
