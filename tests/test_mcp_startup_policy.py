"""M17 MCP 有界并行启动与 required/optional 语义。"""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack

import pytest

from assistant_agent.config.schema import MCPConfig, MCPServerConfig
from assistant_agent.integrations.mcp.manager import MCPManager, _Server, _StartupResult
from assistant_agent.integrations.mcp.status import MCPRequiredServerError
from assistant_agent.observability import NullLogger


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = ""
        self.inputSchema = {"type": "object", "properties": {}}
        self.outputSchema = None
        self.annotations = None


class _Listed:
    def __init__(self, name: str) -> None:
        self.tools = [_Tool(name)]


def _manager(config: MCPConfig) -> MCPManager:
    return MCPManager(config, NullLogger())


def test_optional_failure_degrades_while_successful_server_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPConfig(
        servers={
            "offline": MCPServerConfig(command="x"),
            "online": MCPServerConfig(command="x"),
        }
    )
    manager = _manager(config)

    async def fake(name, _cfg, _semaphore):
        if name == "offline":
            return _StartupResult(category="connection")
        return _StartupResult(
            server=_Server(name, AsyncExitStack(), object()), listed=_Listed("search")
        )

    monkeypatch.setattr(manager, "_connect_and_list", fake)
    try:
        tools = manager.start()
        assert [tool.name for tool in tools] == ["mcp__online__search"]
        statuses = {item.name: item for item in manager.server_statuses()}
        assert statuses["offline"].status == "degraded_connection"
        assert statuses["online"].status == "connected"
    finally:
        manager.close()


def test_required_failure_closes_other_connected_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPConfig(
        servers={
            "required": MCPServerConfig(command="x", startup="required"),
            "online": MCPServerConfig(command="x"),
        }
    )
    manager = _manager(config)
    connected = _Server("online", AsyncExitStack(), object())

    async def fake(name, _cfg, _semaphore):
        if name == "required":
            return _StartupResult(category="timeout")
        return _StartupResult(server=connected, listed=_Listed("ok"))

    monkeypatch.setattr(manager, "_connect_and_list", fake)
    try:
        with pytest.raises(MCPRequiredServerError) as raised:
            manager.start()
        assert raised.value.server == "required"
        assert manager.server_summary() == []
        assert manager.server_statuses()[0].status == "required_failed"
    finally:
        manager.close()


def test_parallel_start_is_bounded_and_registration_order_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPConfig(
        connect_parallelism=3,
        servers={name: MCPServerConfig(command="x") for name in ("first", "second", "third")},
    )
    manager = _manager(config)

    async def fake(name, _cfg, semaphore):
        delays = {"first": 0.12, "second": 0.04, "third": 0.08}
        async with semaphore:
            await asyncio.sleep(delays[name])
        return _StartupResult(
            server=_Server(name, AsyncExitStack(), object()), listed=_Listed(name)
        )

    monkeypatch.setattr(manager, "_connect_and_list", fake)
    started = time.monotonic()
    try:
        tools = manager.start()
        elapsed = time.monotonic() - started
        assert elapsed < 0.22
        assert [tool.name for tool in tools] == [
            "mcp__first__first",
            "mcp__second__second",
            "mcp__third__third",
        ]
    finally:
        manager.close()


def test_connect_timeout_defaults_to_call_timeout_for_legacy_config() -> None:
    server = MCPServerConfig(command="x", timeout=17)
    assert server.connect_timeout is None
    assert server.startup == "optional"


def test_call_timeout_remains_independent_from_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPConfig(
        servers={"server": MCPServerConfig(command="x", connect_timeout=1, timeout=17)}
    )
    manager = _manager(config)

    async def fake(name, _cfg, _semaphore):
        return _StartupResult(
            server=_Server(name, AsyncExitStack(), object()), listed=_Listed("tool")
        )

    monkeypatch.setattr(manager, "_connect_and_list", fake)
    try:
        tool = manager.start()[0]
        assert tool._timeout == 17.0
    finally:
        manager.close()


def test_cancel_during_discovery_closes_connected_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        manager = _manager(MCPConfig(servers={"server": MCPServerConfig(command="x")}))
        closed = asyncio.Event()
        discovery_started = asyncio.Event()

        class _Context:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                closed.set()

        class _BlockingSession:
            async def list_tools(self):
                discovery_started.set()
                await asyncio.Event().wait()

        stack = AsyncExitStack()
        await stack.enter_async_context(_Context())

        async def connected(_name, _cfg):
            return _Server("server", stack, _BlockingSession())

        monkeypatch.setattr(manager, "_connect_one", connected)
        task = asyncio.create_task(
            manager._connect_and_list(
                "server", manager._config.servers["server"], asyncio.Semaphore(1)
            )
        )
        await asyncio.wait_for(discovery_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()

    asyncio.run(exercise())
