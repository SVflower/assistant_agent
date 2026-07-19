"""M7b/M7c MCP client 单测（第一层：进程内 fake session，不依赖 Node/网络）。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import pytest

from assistant_agent.config.schema import MCPConfig, MCPServerConfig, MCPToolPolicyConfig
from assistant_agent.integrations.mcp import MCPManager, MCPTool, extract_result
from assistant_agent.integrations.mcp.discovery import _sanitize
from assistant_agent.integrations.mcp.manager import _Server
from assistant_agent.integrations.mcp.transport import _interpolate_env, _managed_args, _minimal_env
from assistant_agent.observability import NullLogger
from assistant_agent.tools.permissions import Capability
from assistant_agent.tools.registry import ToolRegistry
from tests.support import ToolContextFixture


# ---- fake MCP 类型 ----
class _FakeTool:
    def __init__(
        self,
        name: str,
        description: str = "desc",
        schema: dict | None = None,
        output_schema: dict | None = None,
        annotations: dict | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}
        self.outputSchema = output_schema
        self.annotations = annotations


class _FakeList:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _FakeContent:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _FakeCallResult:
    def __init__(
        self, content: list, is_error: bool = False, structured: Any | None = None
    ) -> None:
        self.content = content
        self.isError = is_error
        self.structuredContent = structured


class _FakeSession:
    def __init__(
        self,
        tools: list[_FakeTool],
        *,
        call_delay: float = 0.0,
        call_exc: Exception | None = None,
        call_result: Any = None,
    ) -> None:
        self._tools = tools
        self._call_delay = call_delay
        self._call_exc = call_exc
        self._call_result = call_result

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _FakeList:
        return _FakeList(self._tools)

    async def call_tool(self, name: str, args: dict, *, meta: dict | None = None) -> Any:
        self.last_meta = meta
        if self._call_delay:
            await asyncio.sleep(self._call_delay)
        if self._call_exc is not None:
            raise self._call_exc
        return self._call_result


def _inject(manager: MCPManager, server: str, session: _FakeSession) -> _Server:
    """把 fake session 塞进 manager，绕过真实子进程。"""
    srv = _Server(name=server, stack=AsyncExitStack(), session=session)
    manager._servers[server] = srv
    return srv


# ---- extract_result（纯函数）----
def test_extract_result_text_join():
    r = _FakeCallResult([_FakeContent("text", "a"), _FakeContent("text", "b")])
    text, is_error, structured = extract_result(r)
    assert text == "a\nb" and is_error is False
    assert structured is None


def test_extract_result_non_text_placeholder():
    r = _FakeCallResult([_FakeContent("image", "")])
    text, _, structured = extract_result(r)
    assert "非文本" in text
    assert structured is None


def test_extract_result_is_error_flag():
    r = _FakeCallResult([_FakeContent("text", "boom")], is_error=True)
    text, is_error, structured = extract_result(r)
    assert is_error is True and text == "boom"
    assert structured is None


def test_extract_structured_only_is_not_empty():
    result = _FakeCallResult([], structured={"count": 2, "items": ["a", "b"]})
    text, is_error, structured = extract_result(result)
    assert is_error is False
    assert '"count": 2' in text
    assert structured == {"count": 2, "items": ["a", "b"]}


# ---- MCPTool.run 权限与错误通道 ----
def _tool(
    caller,
    *,
    auto_approve=False,
    timeout=5.0,
    server="srv",
    raw="do",
    trusted_readonly=False,
    outcome_unknown=True,
    output_schema=None,
):
    return MCPTool(
        server=server,
        registered_name=f"mcp__{server}__{raw}",
        raw_tool=raw,
        description="d",
        input_schema={"type": "object"},
        caller=caller,
        timeout=timeout,
        auto_approve=auto_approve,
        trusted_readonly=trusted_readonly,
        outcome_unknown_on_transport_error=outcome_unknown,
        output_schema=output_schema,
    )


def _execute(tool, args, ctx):
    registry = ToolRegistry()
    registry.register(tool)
    return registry.execute(tool.name, args, ctx)


def test_run_requires_confirm_and_denies():
    calls = []
    tool = _tool(lambda *a: calls.append(a))
    ctx = ToolContextFixture(confirm=lambda _m: "deny")
    res = _execute(tool, {}, ctx)
    assert res.is_error and "拒绝" in res.output and not calls  # 拒绝时不真正调用


def test_run_confirm_category_is_server_tool_scoped():
    ctx = ToolContextFixture(confirm=lambda _m: "always")
    tool_a = _tool(lambda *a: _FakeCallResult([_FakeContent("text", "A")]), raw="ta")
    _execute(tool_a, {}, ctx)
    scopes = {scope.target for scope in ctx.permission_grants}
    assert "srv/ta" in scopes
    assert "srv/tb" not in scopes


def test_run_session_tool_grant_ignores_argument_changes():
    prompts = []
    ctx = ToolContextFixture(confirm=lambda message: prompts.append(message) or "always")
    tool = _tool(lambda *_a: _FakeCallResult([_FakeContent("text", "ok")]), raw="click")
    assert not _execute(tool, {"target": "first"}, ctx).is_error
    assert not _execute(tool, {"target": "second"}, ctx).is_error
    assert len(prompts) == 1
    assert "first" in prompts[0]


def test_run_forwards_stable_correlation_metadata():
    captured = {}

    class CorrelationLogger(NullLogger):
        def correlation_context(self):
            return {"trace_id": "trace-1", "session_id": "session-1", "run_id": "run-1"}

    def caller(_server, _tool_name, _args, _timeout, meta):
        captured.update(meta)
        return _FakeCallResult([_FakeContent("text", "ok")])

    tool = _tool(caller, auto_approve=True)
    result = tool.run({}, ToolContextFixture(logger=CorrelationLogger(), current_call_id="call-1"))
    assert result.code == "ok"
    assert captured == {
        "trace_id": "trace-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "call_id": "call-1",
    }


def test_run_server_session_grant_covers_other_tools_only_on_same_server():
    prompts = []

    def scoped(message, _label):
        prompts.append(message)
        return "broader"

    ctx = ToolContextFixture(confirm_scoped=scoped)

    def result(*_args):
        return _FakeCallResult([_FakeContent("text", "ok")])

    assert not _execute(_tool(result, server="srv", raw="a"), {}, ctx).is_error
    assert not _execute(_tool(result, server="srv", raw="b"), {}, ctx).is_error
    other = _execute(_tool(result, server="other", raw="a"), {}, ctx)
    assert not other.is_error
    assert len(prompts) == 2


def test_mcp_permission_is_single_aggregate_request_without_fake_network_gate():
    requests = _tool(lambda *_a: None).permission_requests(
        {"url": "https://example.com"}, ToolContextFixture()
    )
    assert len(requests) == 1
    assert requests[0].capability == Capability.MCP_CALL
    assert "args=" not in requests[0].target
    assert "args=" in requests[0].display_target


def test_run_auto_approve_skips_confirm():
    tool = _tool(lambda *a: _FakeCallResult([_FakeContent("text", "ok")]), auto_approve=True)
    ctx = ToolContextFixture(confirm=lambda _m: "deny")  # 即便回调拒绝，auto_approve 也跳过
    res = _execute(tool, {}, ctx)
    assert not res.is_error and res.output == "ok"


def test_permission_target_redacts_and_limits_nested_args():
    tool = _tool(lambda *a: None)
    requests = tool.permission_requests(
        {"nested": {"api_token": "secret-value"}, "payload": "x" * 2000}, ToolContextFixture()
    )
    target = requests[0].display_target
    assert "secret-value" not in target
    assert "REDACTED" in target
    assert len(target) < 1100


def test_run_protocol_exception_becomes_error():
    def boom(*_a):
        raise RuntimeError("conn reset")

    tool = _tool(boom, auto_approve=True)
    res = tool.run({}, ToolContextFixture())
    assert res.is_error and res.code == "mcp_outcome_unknown" and res.retryable is False


def test_run_timeout_becomes_error():
    def slow(*_a):
        raise TimeoutError()

    tool = _tool(slow, auto_approve=True)
    res = tool.run({}, ToolContextFixture(current_call_id="call-timeout"))
    assert res.is_error and res.code == "mcp_outcome_unknown" and res.retryable is False
    assert "call_id=call-timeout" in res.output
    assert res.metadata["correlation"]["call_id"] == "call-timeout"


def test_trusted_readonly_transport_error_is_retryable():
    def boom(*_args):
        raise RuntimeError("reset")

    tool = _tool(boom, trusted_readonly=True, outcome_unknown=False)
    result = tool.run({}, ToolContextFixture())
    assert result.code == "mcp_transport_error"
    assert result.retryable is True


def test_run_tool_iserror_feeds_back():
    tool = _tool(
        lambda *a: _FakeCallResult([_FakeContent("text", "bad args")], is_error=True),
        auto_approve=True,
    )
    res = tool.run({}, ToolContextFixture())
    assert res.is_error and res.output == "bad args"  # 执行错误回喂模型
    assert res.code == "mcp_tool_error"


def test_iserror_does_not_require_success_output_schema():
    tool = _tool(
        lambda *_args: _FakeCallResult([_FakeContent("text", "business rejected")], is_error=True),
        output_schema={"type": "object", "required": ["ok"]},
    )
    result = tool.run({}, ToolContextFixture())
    assert result.code == "mcp_tool_error"
    assert "business rejected" in result.output


def test_run_preserves_structured_content_and_output_schema_hash():
    tool = MCPTool(
        server="srv",
        registered_name="mcp__srv__structured",
        raw_tool="structured",
        description="d",
        input_schema={"type": "object"},
        output_schema={"type": "object", "properties": {"value": {"type": "integer"}}},
        caller=lambda *_args: _FakeCallResult([], structured={"value": 42}),
        timeout=5,
        auto_approve=True,
    )
    result = tool.run({}, ToolContextFixture())
    assert result.code == "ok"
    assert result.metadata["structured_content"] == {"value": 42}
    assert len(result.metadata["output_schema_hash"]) == 16
    assert "42" in result.output


def test_output_schema_mismatch_is_contract_error():
    tool = _tool(
        lambda *_args: _FakeCallResult([], structured={"value": "wrong"}),
        output_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )
    result = tool.run({}, ToolContextFixture())
    assert result.code == "mcp_contract_error"
    assert result.retryable is False


# ---- helper ----
def test_sanitize():
    assert _sanitize("a-b.c/d") == "a_b_c_d"


def test_interpolate_env(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    out = _interpolate_env({"A": "${TOK}", "B": "plain", "C": "${MISSING}"})
    assert out == {"A": "secret", "B": "plain", "C": ""}


# ---- manager 发现/过滤/命名空间（fake session）----
def _mgr(servers: dict, **mcp_kw) -> MCPManager:
    cfg = MCPConfig(servers=servers, **mcp_kw)
    from assistant_agent.observability import NullLogger

    return MCPManager(cfg, NullLogger())


def test_discover_namespaces_and_registers():
    m = _mgr({"web": MCPServerConfig(command="x")})
    srv = _inject(m, "web", _FakeSession([_FakeTool("nav"), _FakeTool("click")]))
    tools = m._discover("web", m._config.servers["web"], srv, set(), budget=0)
    names = {t.name for t in tools}
    assert names == {"mcp__web__nav", "mcp__web__click"}


def test_discover_skips_invalid_external_schema():
    config = MCPServerConfig(command="x")
    manager = _mgr({"web": config})
    invalid = _FakeTool("bad", schema={"type": "not-valid"})
    server = _inject(manager, "web", _FakeSession([invalid, _FakeTool("good")]))
    tools = manager._discover("web", config, server, set(), budget=0)
    assert [tool.name for tool in tools] == ["mcp__web__good"]
    assert any("schema 无效" in warning for warning in manager.warnings)


def test_discover_include_exclude():
    cfg = MCPServerConfig(command="x", include_tools=["nav"], exclude_tools=[])
    m = _mgr({"web": cfg})
    srv = _inject(m, "web", _FakeSession([_FakeTool("nav"), _FakeTool("click")]))
    tools = m._discover("web", cfg, srv, set(), budget=0)
    assert [t.name for t in tools] == ["mcp__web__nav"]


def test_untrusted_readonly_annotation_does_not_lower_replay_risk():
    cfg = MCPServerConfig(command="x", trust_tool_annotations=False)
    manager = _mgr({"web": cfg})
    server = _inject(
        manager,
        "web",
        _FakeSession([_FakeTool("read", annotations={"readOnlyHint": True})]),
    )
    tool = manager._discover("web", cfg, server, set(), budget=0)[0]
    request = tool.permission_requests({}, ToolContextFixture())[0]
    assert request.metadata["trusted_readonly"] is False


def test_trusted_annotations_and_policy_control_tool_semantics():
    cfg = MCPServerConfig(
        command="x",
        trust_tool_annotations=True,
        tool_policies={
            "write": MCPToolPolicyConfig(
                replay="requires_decision", outcome_on_transport_error="unknown", timeout=90
            )
        },
    )
    manager = _mgr({"owned": cfg})
    server = _inject(
        manager,
        "owned",
        _FakeSession(
            [
                _FakeTool("read", annotations={"readOnlyHint": True}),
                _FakeTool("write", annotations={"destructiveHint": True}),
            ]
        ),
    )
    read, write = manager._discover("owned", cfg, server, set(), budget=0)
    read_request = read.permission_requests({}, ToolContextFixture())[0]
    write_request = write.permission_requests({}, ToolContextFixture())[0]
    assert read_request.metadata["trusted_readonly"] is True
    assert write_request.metadata["trusted_readonly"] is False
    assert write._timeout == 90


def test_destructive_annotation_overrides_erroneous_readonly_policy():
    cfg = MCPServerConfig(
        command="x",
        auto_approve=True,
        tool_policies={"write": MCPToolPolicyConfig(replay="safe_readonly")},
    )
    manager = _mgr({"owned": cfg})
    server = _inject(
        manager,
        "owned",
        _FakeSession([_FakeTool("write", annotations={"destructiveHint": True})]),
    )
    tool = manager._discover("owned", cfg, server, set(), budget=0)[0]
    request = tool.permission_requests({}, ToolContextFixture())[0]
    assert request.metadata["trusted_readonly"] is False
    assert request.metadata["trusted_server"] is False


def test_discover_per_server_cap():
    cfg = MCPServerConfig(command="x", max_tools=1)
    m = _mgr({"web": cfg})
    srv = _inject(m, "web", _FakeSession([_FakeTool("a"), _FakeTool("b")]))
    tools = m._discover("web", cfg, srv, set(), budget=0)
    assert len(tools) == 1 and any("上限" in w for w in m.warnings)


def test_discover_collision_gets_suffix():
    cfg = MCPServerConfig(command="x")
    m = _mgr({"web": cfg})
    srv = _inject(m, "web", _FakeSession([_FakeTool("a-b"), _FakeTool("a.b")]))
    tools = m._discover("web", cfg, srv, set(), budget=0)
    names = [t.name for t in tools]
    assert names[0] == "mcp__web__a_b" and names[1] == "mcp__web__a_b_2"


def test_sync_bridge_call_and_close():
    m = _mgr({"web": MCPServerConfig(command="x", timeout=5)})
    session = _FakeSession(
        [_FakeTool("do")], call_result=_FakeCallResult([_FakeContent("text", "done")])
    )
    _inject(m, "web", session)
    # 真实同步桥：起 loop 线程，把 call_tool 投进去
    result = m._call_tool("web", "do", {}, 5.0, {"call_id": "call-1"})
    text, is_error, structured = extract_result(result)
    assert text == "done" and not is_error
    assert structured is None
    assert session.last_meta == {"call_id": "call-1"}
    m.close()  # 干净关闭不抛
    assert m._loop is None


def test_sync_bridge_timeout_cancels():
    m = _mgr({"web": MCPServerConfig(command="x")})
    _inject(m, "web", _FakeSession([_FakeTool("do")], call_delay=2.0))
    with pytest.raises(TimeoutError):
        m._call_tool("web", "do", {}, 0.2)
    m.close()


def test_start_disabled_returns_empty():
    m = _mgr({"web": MCPServerConfig(command="x")}, enabled=False)
    assert m.start() == []


def test_connect_failure_closes_partial_stack(monkeypatch):
    import mcp

    record = {"closed": False}

    class FailingSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            record["closed"] = True

        async def initialize(self):
            raise RuntimeError("initialize failed")

    async def fake_open(_stack, _name, _cfg):
        return "r", "w"

    monkeypatch.setattr(mcp, "ClientSession", FailingSession)
    manager = _mgr({"s": MCPServerConfig(command="x")})
    monkeypatch.setattr(manager, "_open_transport", fake_open)

    with pytest.raises(RuntimeError, match="initialize failed"):
        manager._submit(manager._connect_one("s", manager._config.servers["s"]), timeout=2)
    assert record["closed"] is True
    manager.close()


# ---- M7c：HTTP transport 工厂分派 ----
class _FakeStreamCM:
    """假 transport async context manager，yield 指定元组，记录被调用。"""

    def __init__(self, record: dict, kind: str, streams: tuple) -> None:
        self._record = record
        self._kind = kind
        self._streams = streams

    async def __aenter__(self):
        self._record["kind"] = self._kind
        return self._streams

    async def __aexit__(self, *_exc):
        self._record["closed"] = True
        return False


def _patch_transports(monkeypatch, record: dict):
    """把 stdio_client（2 元组）与 streamablehttp_client（3 元组）换成假 CM。"""
    import mcp.client.stdio as stdio_mod
    import mcp.client.streamable_http as http_mod

    def fake_stdio(params, errlog=None):
        record["params"] = params
        record["errlog"] = errlog
        return _FakeStreamCM(record, "stdio", ("r", "w"))

    def fake_http(url, headers=None):
        record["url"] = url
        record["headers"] = headers
        return _FakeStreamCM(record, "http", ("r", "w", lambda: "sid"))

    monkeypatch.setattr(stdio_mod, "stdio_client", fake_stdio)
    monkeypatch.setattr(http_mod, "streamablehttp_client", fake_http)


def _run_open(m: MCPManager, cfg: MCPServerConfig, name: str = "server") -> tuple:
    """在 manager 的 loop 线程里跑 _open_transport，返回 (read, write)。"""

    async def _go():
        async with AsyncExitStack() as stack:
            return await m._open_transport(stack, name, cfg)

    return m._submit(_go(), timeout=5)


def test_http_transport_dispatch(monkeypatch):
    record: dict = {}
    _patch_transports(monkeypatch, record)
    m = _mgr({"h": MCPServerConfig(type="http", url="https://x/mcp")})
    rw = _run_open(m, m._config.servers["h"])
    assert record["kind"] == "http"  # 走 http 工厂
    assert rw == ("r", "w")  # 丢弃 get_session_id，只留 read/write
    m.close()


def test_stdio_transport_dispatch(monkeypatch, tmp_path):
    record: dict = {}
    _patch_transports(monkeypatch, record)
    cfg = MCPConfig(servers={"s": MCPServerConfig(type="stdio", command="npx")})
    m = MCPManager(
        cfg,
        NullLogger(),
        artifact_root=tmp_path / "artifacts",
        stderr_root=tmp_path / "stderr",
    )
    rw = _run_open(m, m._config.servers["s"], "s")
    assert record["kind"] == "stdio" and rw == ("r", "w")
    assert record["params"].cwd == tmp_path / "artifacts" / "s"
    assert record["params"].env["ASSISTANT_AGENT_ARTIFACT_DIR"] == str(tmp_path / "artifacts" / "s")
    assert (tmp_path / "stderr" / "s" / "server.log").is_file()
    m.close()


def test_playwright_args_route_outputs_and_explicit_output_wins(tmp_path):
    args = _managed_args("npx", ["-y", "@playwright/mcp@1.2.3"], tmp_path)
    assert args[-4:] == ["--output-dir", str(tmp_path), "--output-max-size", "104857600"]
    explicit = ["@playwright/mcp", "--output-dir", "custom"]
    assert _managed_args("npx", explicit, tmp_path) == explicit


def test_minimal_env_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "bin")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    env = _minimal_env()
    assert env["PATH"] == "bin"
    assert "GITHUB_TOKEN" not in env


def test_http_headers_interpolated(monkeypatch):
    monkeypatch.setenv("TOK", "secret123")
    record: dict = {}
    _patch_transports(monkeypatch, record)
    cfg = MCPServerConfig(
        type="http", url="https://x/mcp", headers={"Authorization": "Bearer ${TOK}"}
    )
    m = _mgr({"h": cfg})
    _run_open(m, cfg)
    assert record["headers"]["Authorization"] == "Bearer secret123"  # ${VAR} 注入
    m.close()


def test_http_reconnect_no_replay(monkeypatch):
    """途中断线：call_tool 抛异常 → 转 error，绝不自动重发（副作用不重复）。"""
    calls = {"n": 0}

    def exploding(*_a):
        calls["n"] += 1
        raise ConnectionError("stream reset mid-call")

    tool = _tool(exploding, auto_approve=True, server="h", raw="write_file")
    res = tool.run({"path": "x"}, ToolContextFixture())
    assert res.is_error and res.code == "mcp_outcome_unknown"
    assert res.retryable is False
    assert calls["n"] == 1  # 只调一次，绝无自动重试
