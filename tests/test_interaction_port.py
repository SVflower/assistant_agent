"""公共同步 InteractionPort 契约。"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest

from assistant_agent.application.runs import SessionRuntime
from assistant_agent.execution import RunControl
from assistant_agent.interaction import (
    ApprovalDecision,
    ApprovalRequest,
    BlockingInteractionPort,
    ContinueRequest,
    QuestionAnswer,
    QuestionRequest,
    SafeDefaultInteractionPort,
)
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.registry import ToolRegistry
from tests.support import Tool, ToolContextFixture, ToolResult


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
    assert published is not None
    assert published.request_id == request.request_id
    assert published.kind == "approval"
    assert datetime.fromisoformat(published.expires_at.replace("Z", "+00:00")) > datetime.now(UTC)
    assert port.pending_requests() == (published,)
    assert port.respond(ApprovalDecision(request.request_id, "allow")) is True
    worker.join(timeout=1)
    assert result == ["allow"]
    assert port.pending_requests() == ()
    assert port.respond(ApprovalDecision(request.request_id, "allow")) is False


def test_wrong_id_type_and_timeout_fail_closed() -> None:
    port = BlockingInteractionPort(timeout=0.05)
    request = ApprovalRequest(run_id="run-1")
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(port.request_approval(request).choice), daemon=True
    )
    worker.start()
    published = port.next_request(timeout=0.5)
    assert published is not None and published.request_id == request.request_id
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
    published = port.next_request(timeout=0.5)
    assert published is not None and published.request_id == request.request_id
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


def test_interrupt_pending_wakes_waiter_without_closing_port() -> None:
    port = BlockingInteractionPort(timeout=5)
    first = ApprovalRequest(run_id="run-1")
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(port.request_approval(first).choice), daemon=True
    )
    worker.start()
    published = port.next_request(timeout=0.5)
    assert published is not None and published.request_id == first.request_id
    port.interrupt_pending()
    worker.join(timeout=1)
    assert result == ["deny"]
    assert port.respond(ApprovalDecision(first.request_id, "allow")) is False

    second = ApprovalRequest(run_id="run-2")
    second_result: list[str] = []
    second_worker = threading.Thread(
        target=lambda: second_result.append(port.request_approval(second).choice), daemon=True
    )
    second_worker.start()
    second_published = port.next_request(timeout=0.5)
    assert second_published is not None and second_published.request_id == second.request_id
    assert port.respond(ApprovalDecision(second.request_id, "allow")) is True
    second_worker.join(timeout=1)
    assert second_result == ["allow"]


def test_interrupted_request_is_not_published_from_stale_queue() -> None:
    port = BlockingInteractionPort(timeout=5)
    request = ApprovalRequest(run_id="run-1")
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(port.request_approval(request).choice), daemon=True
    )
    worker.start()
    time.sleep(0.02)
    port.interrupt_pending()
    worker.join(timeout=1)
    assert result == ["deny"]
    assert port.next_request(timeout=0.01) is None
    assert port.pending_requests() == ()
    assert port.respond(ApprovalDecision(request.request_id, "allow")) is False


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_session_control_interrupts_pending_interaction(action: str) -> None:
    port = BlockingInteractionPort(timeout=5)
    control = RunControl()

    class RuntimeStub:
        run_control = control
        interaction = port

    session_runtime = SessionRuntime.__new__(SessionRuntime)
    session_runtime.runtime = RuntimeStub()  # type: ignore[assignment]
    session_runtime._lock = threading.Lock()  # noqa: SLF001
    session_runtime._active_run_id = "run-1"  # noqa: SLF001
    result: list[str] = []
    request = ApprovalRequest(run_id="run-1")
    worker = threading.Thread(
        target=lambda: result.append(port.request_approval(request).choice), daemon=True
    )
    worker.start()
    assert port.next_request(timeout=0.5) is not None

    getattr(session_runtime, action)()

    worker.join(timeout=1)
    assert control.pause_requested is True
    assert control.cancel_requested is (action == "cancel")
    assert result == ["deny"]


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
    context = ToolContextFixture(
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
    context = ToolContextFixture(
        workspace_root=tmp_path,
        interaction=BrokenPort(),
        permission_policy=PermissionPolicy(mode="strict"),
    )
    result = registry.execute(tool.name, {}, context, call_id="call-1")
    assert result.code == "permission_denied"
    assert tool.called is False
