"""M7b MCP client 单测（第一层：进程内 fake session，不依赖 Node/网络）。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import pytest

from assistant_agent.config.schema import MCPConfig, MCPServerConfig
from assistant_agent.mcp import MCPManager, MCPTool, extract_content
from assistant_agent.mcp.manager import _interpolate_env, _sanitize, _Server
from assistant_agent.tools.base import ToolContext


# ---- fake MCP 类型 ----
class _FakeTool:
    def __init__(self, name: str, description: str = "desc", schema: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _FakeList:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _FakeContent:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _FakeCallResult:
    def __init__(self, content: list, is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


class _FakeSession:
    def __init__(self, tools: list[_FakeTool], *, call_delay: float = 0.0,
                 call_exc: Exception | None = None, call_result: Any = None) -> None:
        self._tools = tools
        self._call_delay = call_delay
        self._call_exc = call_exc
        self._call_result = call_result

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _FakeList:
        return _FakeList(self._tools)

    async def call_tool(self, name: str, args: dict) -> Any:
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


# ---- extract_content（纯函数）----
def test_extract_content_text_join():
    r = _FakeCallResult([_FakeContent("text", "a"), _FakeContent("text", "b")])
    text, is_error = extract_content(r)
    assert text == "a\nb" and is_error is False


def test_extract_content_non_text_placeholder():
    r = _FakeCallResult([_FakeContent("image", "")])
    text, _ = extract_content(r)
    assert "非文本" in text


def test_extract_content_is_error_flag():
    r = _FakeCallResult([_FakeContent("text", "boom")], is_error=True)
    text, is_error = extract_content(r)
    assert is_error is True and text == "boom"


# ---- MCPTool.run 权限与错误通道 ----
def _tool(caller, *, auto_approve=False, timeout=5.0, server="srv", raw="do"):
    return MCPTool(server=server, registered_name=f"mcp__{server}__{raw}", raw_tool=raw,
                   description="d", input_schema={"type": "object"}, caller=caller,
                   timeout=timeout, auto_approve=auto_approve)


def test_run_requires_confirm_and_denies():
    calls = []
    tool = _tool(lambda *a: calls.append(a))
    ctx = ToolContext(confirm=lambda _m: "deny")
    res = tool.run({}, ctx)
    assert res.is_error and "拒绝" in res.output and not calls  # 拒绝时不真正调用


def test_run_confirm_category_is_server_tool_scoped():
    ctx = ToolContext(confirm=lambda _m: "always")
    tool_a = _tool(lambda *a: _FakeCallResult([_FakeContent("text", "A")]), raw="ta")
    tool_a.run({}, ctx)
    # server+tool 粒度：对 ta 永久允许后 category 为 mcp:srv:ta，不放行同 server 别的工具
    assert "mcp:srv:ta" in ctx.always_allowed
    assert "mcp:srv:tb" not in ctx.always_allowed


def test_run_auto_approve_skips_confirm():
    tool = _tool(lambda *a: _FakeCallResult([_FakeContent("text", "ok")]), auto_approve=True)
    ctx = ToolContext(confirm=lambda _m: "deny")  # 即便回调拒绝，auto_approve 也跳过
    res = tool.run({}, ctx)
    assert not res.is_error and res.output == "ok"


def test_run_protocol_exception_becomes_error():
    def boom(*_a):
        raise RuntimeError("conn reset")
    tool = _tool(boom, auto_approve=True)
    res = tool.run({}, ToolContext())
    assert res.is_error and "调用失败" in res.output


def test_run_timeout_becomes_error():
    def slow(*_a):
        raise TimeoutError()
    tool = _tool(slow, auto_approve=True)
    res = tool.run({}, ToolContext())
    assert res.is_error and "超时" in res.output


def test_run_tool_iserror_feeds_back():
    tool = _tool(lambda *a: _FakeCallResult([_FakeContent("text", "bad args")], is_error=True),
                 auto_approve=True)
    res = tool.run({}, ToolContext())
    assert res.is_error and res.output == "bad args"  # 执行错误回喂模型


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
    from assistant_agent.obs import NullLogger
    return MCPManager(cfg, NullLogger())


def test_discover_namespaces_and_registers():
    m = _mgr({"web": MCPServerConfig(command="x")})
    srv = _inject(m, "web", _FakeSession([_FakeTool("nav"), _FakeTool("click")]))
    tools = m._discover("web", m._config.servers["web"], srv, set(), budget=0)
    names = {t.name for t in tools}
    assert names == {"mcp__web__nav", "mcp__web__click"}


def test_discover_include_exclude():
    cfg = MCPServerConfig(command="x", include_tools=["nav"], exclude_tools=[])
    m = _mgr({"web": cfg})
    srv = _inject(m, "web", _FakeSession([_FakeTool("nav"), _FakeTool("click")]))
    tools = m._discover("web", cfg, srv, set(), budget=0)
    assert [t.name for t in tools] == ["mcp__web__nav"]


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
    session = _FakeSession([_FakeTool("do")],
                           call_result=_FakeCallResult([_FakeContent("text", "done")]))
    _inject(m, "web", session)
    # 真实同步桥：起 loop 线程，把 call_tool 投进去
    result = m._call_tool("web", "do", {}, 5.0)
    text, is_error = extract_content(result)
    assert text == "done" and not is_error
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
