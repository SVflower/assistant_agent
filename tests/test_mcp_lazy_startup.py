"""M20 optional MCP 工具目录、惰性连接和后台发现。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from assistant_agent.bootstrap.tools import start_mcp
from assistant_agent.config.schema import MCPConfig, MCPServerConfig
from assistant_agent.execution import RunControl
from assistant_agent.integrations.mcp.catalog import MCPToolCatalog, server_config_fingerprint
from assistant_agent.integrations.mcp.discovery import MCPToolDefinition
from assistant_agent.integrations.mcp.manager import MCPManager, _Server, _StartupResult
from assistant_agent.observability import NullLogger
from assistant_agent.tools.registry import ToolRegistry


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} description"
        self.inputSchema = {"type": "object", "properties": {}}
        self.outputSchema = None
        self.annotations = None


class _Listed:
    def __init__(self, name: str) -> None:
        self.tools = [_Tool(name)]


class _Result:
    content = []
    isError = False
    structuredContent = None


class _Session:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, _name, _args, meta=None):
        self.calls += 1
        return _Result()


class _Stack:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _definition(name: str = "search") -> tuple[MCPToolDefinition, ...]:
    return (
        MCPToolDefinition(
            raw_name=name,
            description="search safely",
            input_schema={"type": "object", "properties": {}},
        ),
    )


def _manager(tmp_path: Path, server: MCPServerConfig) -> tuple[MCPManager, MCPToolCatalog]:
    catalog = MCPToolCatalog(tmp_path / "catalog")
    return (
        MCPManager(
            MCPConfig(servers={"demo": server}),
            NullLogger(),
            artifact_root=tmp_path / "artifacts",
            stderr_root=tmp_path / "stderr",
            catalog=catalog,
        ),
        catalog,
    )


def test_catalog_round_trip_is_fingerprinted_and_contains_no_config_secrets(tmp_path: Path) -> None:
    server = MCPServerConfig(command="demo", env={"TOKEN": "${MCP_TOKEN}"})
    catalog = MCPToolCatalog(tmp_path)
    catalog.save("demo", server, _definition())

    snapshot = catalog.load("demo", server)
    assert snapshot is not None and snapshot.definitions[0].raw_name == "search"
    changed = server.model_copy(update={"args": ["--changed"]})
    assert catalog.load("demo", changed) is None
    stored = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "MCP_TOKEN" not in stored
    assert "TOKEN" not in stored


def test_catalog_fingerprint_ignores_secret_values_but_tracks_tool_configuration() -> None:
    first = MCPServerConfig(command="demo", env={"TOKEN": "first"}, headers={"Auth": "one"})
    rotated = MCPServerConfig(command="demo", env={"TOKEN": "second"}, headers={"Auth": "two"})
    changed = first.model_copy(update={"args": ["--changed"]})

    assert server_config_fingerprint(first) == server_config_fingerprint(rotated)
    assert server_config_fingerprint(first) != server_config_fingerprint(changed)


def test_optional_cached_tools_do_not_connect_during_runtime_start(tmp_path: Path) -> None:
    server = MCPServerConfig(command="never-run")
    manager, catalog = _manager(tmp_path, server)
    catalog.save("demo", server, _definition())
    called = False

    async def fail_if_called(*_args):
        nonlocal called
        called = True
        raise AssertionError("optional cached server must not start")

    manager._connect_and_list = fail_if_called  # type: ignore[method-assign]
    try:
        tools = manager.start_runtime()
        assert [tool.name for tool in tools] == ["mcp__demo__search"]
        assert manager.server_statuses()[0].status == "available_cached"
        assert called is False
    finally:
        manager.close()


def test_optional_cached_tool_connects_on_first_call_and_reuses_session(tmp_path: Path) -> None:
    server = MCPServerConfig(command="demo")
    manager, catalog = _manager(tmp_path, server)
    catalog.save("demo", server, _definition())
    session = _Session()
    stack = _Stack()
    connects = 0

    async def connect(name, _cfg, _semaphore):
        nonlocal connects
        connects += 1
        return _StartupResult(server=_Server(name, stack, session), listed=_Listed("search"))

    manager._connect_and_list = connect  # type: ignore[method-assign]
    try:
        manager.start_runtime()
        manager._call_tool("demo", "search", {}, 1.0)
        manager._call_tool("demo", "search", {}, 1.0)
        assert connects == 1
        assert session.calls == 2
        assert manager.server_statuses()[0].status == "connected"
    finally:
        manager.close()
    assert stack.closed is True


def test_lazy_connection_keeps_filtered_capability_tool_names(tmp_path: Path) -> None:
    server = MCPServerConfig(command="demo", include_tools=["search"])
    manager, catalog = _manager(tmp_path, server)
    catalog.save("demo", server, _definition())
    session = _Session()

    async def connect(name, _cfg, _semaphore):
        listed = SimpleNamespace(tools=[_Listed("search").tools[0], _Listed("hidden").tools[0]])
        return _StartupResult(server=_Server(name, _Stack(), session), listed=listed)

    manager._connect_and_list = connect  # type: ignore[method-assign]
    try:
        manager.start_runtime()
        manager._call_tool("demo", "search", {}, 1.0)
        assert manager.server_statuses()[0].tool_names == ("search",)
    finally:
        manager.close()


def test_optional_without_catalog_discovers_in_background_for_next_runtime(tmp_path: Path) -> None:
    server = MCPServerConfig(command="demo")
    manager, catalog = _manager(tmp_path, server)
    stack = _Stack()

    async def connect(name, _cfg, _semaphore):
        await asyncio.sleep(0.01)
        return _StartupResult(server=_Server(name, stack, _Session()), listed=_Listed("search"))

    manager._connect_and_list = connect  # type: ignore[method-assign]
    try:
        started = time.monotonic()
        assert manager.start_runtime() == []
        assert time.monotonic() - started < 0.1
        deadline = time.monotonic() + 2
        while manager.server_statuses()[0].status == "discovering":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert manager.server_statuses()[0].status == "restart_required"
        assert catalog.load("demo", server) is not None
        assert stack.closed is True
    finally:
        manager.close()


def test_corrupt_catalog_is_ignored_without_exposing_tools(tmp_path: Path) -> None:
    server = MCPServerConfig(command="demo")
    manager, catalog = _manager(tmp_path, server)
    catalog.root.mkdir(parents=True)
    path = catalog._path("demo")
    path.write_text(json.dumps({"version": 1, "server": "demo", "tools": []}), encoding="utf-8")

    async def blocked(*_args):
        await asyncio.Event().wait()

    manager._connect_and_list = blocked  # type: ignore[method-assign]
    try:
        assert manager.start_runtime() == []
        assert manager.server_statuses()[0].status == "discovering"
    finally:
        manager.close()


def test_optional_mcp_schema_budget_omits_tools_without_failing_runtime(tmp_path: Path) -> None:
    server = MCPServerConfig(command="demo")
    catalog_root = tmp_path / "catalog"
    MCPToolCatalog(catalog_root).save("demo", server, _definition())
    registry = ToolRegistry()

    manager, notices = start_mcp(
        MCPConfig(servers={"demo": server}),
        registry,
        NullLogger(),
        artifact_root=tmp_path / "artifacts",
        stderr_root=tmp_path / "stderr",
        catalog_root=catalog_root,
        run_control=RunControl(),
        workspace_root=tmp_path,
        allowed_transports=frozenset({"stdio"}),
        max_tools_schema_tokens=0,
    )
    try:
        assert registry.names() == []
        assert manager is not None
        assert manager.server_capabilities()[0].status == "available_cached"
        assert manager.server_capabilities()[0].tool_names == ()
        notice = next(item for item in notices if item.code == "mcp_tools_omitted_context_limit")
        assert notice.details["count"] == 1
    finally:
        if manager is not None:
            manager.close()
