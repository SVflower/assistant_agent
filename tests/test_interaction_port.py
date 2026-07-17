"""公共同步 InteractionPort 契约。"""

from __future__ import annotations

import threading
import time

from assistant_agent.interaction import (
    ApprovalDecision,
    ApprovalRequest,
    BlockingInteractionPort,
    ContinueRequest,
    QuestionAnswer,
    QuestionRequest,
    SafeDefaultInteractionPort,
)
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.registry import ToolRegistry


def test_safe_default_never_allows() -> None:
    port = SafeDefaultInteractionPort()
    approval = ApprovalRequest(run_id="run-1")
    question = QuestionRequest(run_id="run-1", question="q", options=("a",))
    continuation = ContinueRequest(run_id="run-1", iterations_used=3, iteration_limit=3)

    assert port.request_approval(approval).choice == "deny"
    assert port.ask_question(question).available is False
    assert port.confirm_continue(continuation).continue_run is False


def test_blocking_port_accepts_response_from_another_thread() -> None:
    port = BlockingInteractionPort(timeout=1)
    request = ApprovalRequest(run_id="run-1", legal_options=("allow", "deny"))
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(port.request_approval(request).choice), daemon=True
    )
    worker.start()

    published = port.next_request(timeout=0.5)
    assert published == request
    assert published.kind == "approval"
    assert port.respond(ApprovalDecision(request.request_id, "allow")) is True
    worker.join(timeout=1)
    assert result == ["allow"]
    assert port.respond(ApprovalDecision(request.request_id, "allow")) is False


def test_wrong_id_type_and_timeout_fail_closed() -> None:
    port = BlockingInteractionPort(timeout=0.05)
    request = ApprovalRequest(run_id="run-1")
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(port.request_approval(request).choice), daemon=True
    )
    worker.start()
    assert port.next_request(timeout=0.5) == request
    assert port.respond(ApprovalDecision("wrong", "allow")) is False
    assert port.respond(QuestionAnswer(request.request_id, answer="yes", available=True)) is False
    worker.join(timeout=1)
    assert result == ["deny"]


def test_close_wakes_waiter_and_rejects_late_response() -> None:
    port = BlockingInteractionPort(timeout=5)
    request = ApprovalRequest(run_id="run-1")
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(port.request_approval(request).choice), daemon=True
    )
    worker.start()
    assert port.next_request(timeout=0.5) == request
    port.close()
    port.close()
    worker.join(timeout=1)
    assert result == ["deny"]
    assert port.respond(ApprovalDecision(request.request_id, "allow")) is False


def test_timeout_does_not_leave_pending_request() -> None:
    port = BlockingInteractionPort(timeout=0.01)
    request = ApprovalRequest(run_id="run-1")
    assert port.request_approval(request).choice == "deny"
    time.sleep(0.01)
    assert port.respond(ApprovalDecision(request.request_id, "allow")) is False


class _EffectTool(Tool):
    name = "effect"
    description = "test"

    def __init__(self) -> None:
        self.called = False

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    def permission_requests(self, args, ctx):
        return [
            PermissionRequest(
                self.name,
                Capability.FILESYSTEM_WRITE,
                "sk-abcdef123456",
                "write risk",
                metadata={"display_target": "api_key=sk-abcdef123456"},
            )
        ]

    def run(self, args, ctx):
        self.called = True
        return ToolResult.ok("done")


class _CaptureApproval(SafeDefaultInteractionPort):
    def __init__(self) -> None:
        self.request = None

    def request_approval(self, request):
        self.request = request
        return ApprovalDecision(request.request_id, "allow")


def test_registry_delivers_structured_redacted_approval_identity(tmp_path) -> None:
    port = _CaptureApproval()
    context = ToolContext(
        workspace_root=tmp_path,
        interaction=port,
        permission_policy=PermissionPolicy(mode="strict"),
    )
    context.bind_run("run-1", "session-1")
    tool = _EffectTool()
    registry = ToolRegistry()
    registry.register(tool)

    result = registry.execute(tool.name, {}, context, call_id="call-1")

    assert result.is_error is False and tool.called is True
    assert port.request is not None
    assert (port.request.run_id, port.request.session_id, port.request.call_id) == (
        "run-1",
        "session-1",
        "call-1",
    )
    assert port.request.capabilities == ("filesystem.write",)
    assert "sk-abcdef123456" not in port.request.display_targets[0]
    assert "***REDACTED***" in port.request.display_targets[0]


def test_interaction_exception_fails_closed(tmp_path) -> None:
    class BrokenPort(_CaptureApproval):
        def request_approval(self, request):
            raise RuntimeError("offline")

    tool = _EffectTool()
    registry = ToolRegistry()
    registry.register(tool)
    context = ToolContext(
        workspace_root=tmp_path,
        interaction=BrokenPort(),
        permission_policy=PermissionPolicy(mode="strict"),
    )
    result = registry.execute(tool.name, {}, context, call_id="call-1")
    assert result.code == "permission_denied"
    assert tool.called is False
