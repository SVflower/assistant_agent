"""M9b 统一权限策略与 Registry 强制门控。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant_agent.observability import NullLogger
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import (
    Capability,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
)
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.registry import ToolRegistry


def _request(target: str = "target") -> PermissionRequest:
    return PermissionRequest("effect", Capability.FILESYSTEM_WRITE, target, "测试副作用")


def test_explicit_priority_is_deny_then_ask_then_allow(tmp_path):
    request = _request(str(tmp_path / "file.txt"))
    policy = PermissionPolicy(
        mode="unrestricted",
        rules=[
            PermissionRule("allow", request.capability, request.target),
            PermissionRule("ask", request.capability, request.target),
            PermissionRule("deny", request.capability, request.target),
        ],
    )
    decision = policy.decide(request, workspace_root=tmp_path, grants=set())
    assert decision.effect == "deny"


def test_exact_grant_does_not_spread_to_other_target(tmp_path):
    first = _request(str(tmp_path / "a.txt"))
    second = _request(str(tmp_path / "b.txt"))
    policy = PermissionPolicy(mode="strict")
    granted = policy.decide(first, workspace_root=tmp_path, grants={first.scope})
    not_granted = policy.decide(second, workspace_root=tmp_path, grants={first.scope})
    assert granted.effect == "allow" and granted.remembered
    assert not_granted.effect == "ask"


def test_broader_grant_is_explicit_and_does_not_use_wildcard_matching(tmp_path):
    broader = PermissionScope(Capability.MCP_CALL, "mcp-server:srv", "srv")
    request = PermissionRequest(
        "mcp__srv__click",
        Capability.MCP_CALL,
        "srv/click",
        "risk",
        broader_scope=broader,
    )
    policy = PermissionPolicy(mode="workspace")
    assert policy.decide(request, workspace_root=tmp_path, grants={broader}).effect == "allow"
    other = PermissionRequest("mcp__other__click", Capability.MCP_CALL, "other/click", "risk")
    assert policy.decide(other, workspace_root=tmp_path, grants={broader}).effect == "ask"


def test_explicit_ask_overrides_exact_session_grant(tmp_path):
    request = _request(str(tmp_path / "file.txt"))
    policy = PermissionPolicy(
        mode="workspace",
        rules=[PermissionRule("ask", request.capability, request.target)],
    )
    decision = policy.decide(request, workspace_root=tmp_path, grants={request.scope})
    assert decision.effect == "ask"


def test_explicit_rule_is_preserved_in_permission_audit(tmp_path):
    class _Recorder(NullLogger):
        def __init__(self):
            self.event = None

        def permission_decision(self, **event):
            self.event = event

    request = _request(str(tmp_path / "file.txt"))
    logger = _Recorder()
    ctx = ToolContext(
        workspace_root=tmp_path,
        logger=logger,
        permission_policy=PermissionPolicy(
            rules=[PermissionRule("allow", request.capability, request.target)]
        ),
    )
    assert ctx.request_permissions([request])
    assert logger.event is not None
    assert logger.event["matched_rules"] == [f"allow:{request.capability.value}:*:{request.target}"]


def test_mode_defaults_keep_file_capabilities_independent(tmp_path):
    inside_read = PermissionRequest(
        "read_file", Capability.FILESYSTEM_READ, str(tmp_path / "in.txt"), "read"
    )
    inside_write = _request(str(tmp_path / "in.txt"))
    outside_read = PermissionRequest(
        "read_file", Capability.FILESYSTEM_READ, str(tmp_path.parent / "out.txt"), "read"
    )
    workspace = PermissionPolicy(mode="workspace")
    assert workspace.decide(inside_read, workspace_root=tmp_path, grants=set()).effect == "allow"
    assert workspace.decide(inside_write, workspace_root=tmp_path, grants=set()).effect == "allow"
    assert workspace.decide(outside_read, workspace_root=tmp_path, grants=set()).effect == "ask"
    assert (
        PermissionPolicy(mode="readonly")
        .decide(inside_write, workspace_root=tmp_path, grants=set())
        .effect
        == "deny"
    )


def test_default_sensitive_roots_remain_denied_when_custom_roots_are_added(tmp_path):
    policy = PermissionPolicy(mode="unrestricted", sensitive_paths=[tmp_path / "extra"])
    ssh_request = PermissionRequest(
        "read_file", Capability.FILESYSTEM_READ, str(Path.home() / ".ssh" / "config"), "read"
    )
    custom_request = PermissionRequest(
        "read_file", Capability.FILESYSTEM_READ, str(tmp_path / "extra" / "secret"), "read"
    )
    assert policy.decide(ssh_request, workspace_root=tmp_path, grants=set()).effect == "deny"
    assert policy.decide(custom_request, workspace_root=tmp_path, grants=set()).effect == "deny"


def test_noninteractive_ask_fails_closed(tmp_path):
    ctx = ToolContext(
        workspace_root=tmp_path,
        permission_policy=PermissionPolicy(mode="strict"),
        interactive=False,
    )
    assert not ctx.request_permissions([_request(str(tmp_path / "file.txt"))])


def test_permission_prompt_deduplicates_shared_risk(tmp_path):
    captured = ""

    def confirm(message: str) -> str:
        nonlocal captured
        captured = message
        return "deny"

    requests = [
        PermissionRequest("shell", Capability.PROCESS_EXECUTE, "cmd", "共享风险"),
        PermissionRequest("shell", Capability.NETWORK_ACCESS, "unknown", "共享风险"),
    ]
    ctx = ToolContext(
        workspace_root=tmp_path,
        permission_policy=PermissionPolicy(mode="workspace"),
        interactive=True,
        confirm=confirm,
    )
    assert not ctx.request_permissions(requests)
    assert captured.count("共享风险") == 1
    assert "process.execute" in captured and "network.access" in captured


class _EffectTool(Tool):
    name = "effect"
    description = "test"

    def __init__(self) -> None:
        self.calls = 0

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def permission_requests(self, args, ctx):
        return [_request()]

    def run(self, args, ctx):
        self.calls += 1
        return ToolResult.ok("done")


class _DenyObserver:
    def pre_tool_use(self, tool, args, requests):
        return "observer blocked"


class _BrokenPreObserver:
    def pre_tool_use(self, tool, args, requests):
        raise RuntimeError("broken")


class _BrokenPostObserver:
    def post_tool_use(self, tool, args, requests, result):
        raise RuntimeError("broken")


class _MutatingObserver:
    def pre_tool_use(self, tool, args, requests):
        args["changed"] = True
        requests.clear()
        return None

    def post_tool_use(self, tool, args, requests, result):
        result.output = "tampered"


def _registry(tool: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def test_pre_observer_can_deny_without_execution():
    tool = _EffectTool()
    ctx = ToolContext(
        permission_policy=PermissionPolicy(mode="unrestricted"),
        pre_tool_observers=[_DenyObserver()],
    )
    result = _registry(tool).execute(tool.name, {}, ctx)
    assert result.is_error and not result.executed
    assert tool.calls == 0


def test_pre_observer_exception_fails_closed():
    tool = _EffectTool()
    ctx = ToolContext(
        permission_policy=PermissionPolicy(mode="unrestricted"),
        pre_tool_observers=[_BrokenPreObserver()],
    )
    result = _registry(tool).execute(tool.name, {}, ctx)
    assert result.is_error and "权限检查失败" in result.output
    assert tool.calls == 0


def test_post_observer_exception_does_not_replace_result():
    class _Recorder(NullLogger):
        def __init__(self):
            self.errors = []

        def observer_error(self, **event):
            self.errors.append(event)

    tool = _EffectTool()
    logger = _Recorder()
    ctx = ToolContext(
        permission_policy=PermissionPolicy(mode="unrestricted"),
        post_tool_observers=[_BrokenPostObserver()],
        logger=logger,
    )
    result = _registry(tool).execute(tool.name, {}, ctx)
    assert result.output == "done" and not result.is_error
    assert tool.calls == 1
    assert logger.errors == [{"phase": "post", "tool": "effect", "error": "broken"}]


def test_observer_mutation_cannot_change_execution_or_result():
    tool = _EffectTool()
    observer = _MutatingObserver()
    ctx = ToolContext(
        permission_policy=PermissionPolicy(mode="unrestricted"),
        pre_tool_observers=[observer],
        post_tool_observers=[observer],
    )
    result = _registry(tool).execute(tool.name, {}, ctx)
    assert result.output == "done"
    assert tool.calls == 1


def test_unknown_extension_tool_defaults_to_confirmation():
    class _UndeclaredTool(_EffectTool):
        def permission_requests(self, args, ctx):
            return Tool.permission_requests(self, args, ctx)

    tool = _UndeclaredTool()
    result = _registry(tool).execute(tool.name, {}, ToolContext(interactive=False))
    assert result.is_error and not result.executed
    assert tool.calls == 0
