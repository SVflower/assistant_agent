"""MCPManager：连接 MCP server（stdio）、发现工具、同步桥、生命周期。

同步/异步桥（最大的坎）：mcp SDK 是 asyncio，我们的 Tool.run() 是同步。
方案：起一个守护线程跑常驻 event loop，连接/持有 ClientSession 都在该 loop 里；
MCPTool.run() 用 run_coroutine_threadsafe 把协程投进去、同步等结果。

这样保留同步 Agent/Tool Port，同时把 SDK 要求的异步 session 终身固定在创建它的 loop。不能为每次
工具调用临时 `asyncio.run()`，否则会跨 loop 使用 session，并破坏取消和统一关闭。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from assistant_agent.contracts.capabilities import MCPServerCapability
from assistant_agent.execution import RunControl, RunInterrupted
from assistant_agent.integrations.mcp.catalog import MCPToolCatalog
from assistant_agent.integrations.mcp.discovery import (
    build_discovered_tools,
    build_tools_from_definitions,
    definitions_from_listed,
)
from assistant_agent.integrations.mcp.status import (
    MCPRequiredServerError,
    MCPServerStatus,
    startup_failure_status,
)
from assistant_agent.integrations.mcp.tool import MCPDependencyUnavailable, MCPTool
from assistant_agent.integrations.mcp.transport import (
    open_transport,
)

if TYPE_CHECKING:
    from assistant_agent.config.schema import MCPConfig, MCPServerConfig
    from assistant_agent.observability import NullLogger


@dataclass
class _Server:
    """一个已连接 server 的运行态。"""

    name: str
    stack: AsyncExitStack
    session: Any
    tool_names: list[str] = field(default_factory=list)


@dataclass
class _StartupResult:
    server: _Server | None = None
    listed: Any = None
    category: str | None = None


class MCPManager:
    """管理所有 MCP server 的生命周期与工具桥接。

    用法：m = MCPManager(config, logger); tools = m.start(); ... ; m.close()
    start() 返回发现并通过过滤/上限的 MCPTool 列表，供 bootstrap 注册进 registry。

    optional server 的配置目录、连接状态和工具可见性由本对象共同维护。关闭时必须先阻止新提交，
    再在所属 event loop 退出 session/transport，最后 join 线程。
    """

    def __init__(
        self,
        config: MCPConfig,
        logger: NullLogger,
        *,
        artifact_root: Path | None = None,
        stderr_root: Path | None = None,
        run_control: RunControl | None = None,
        workspace_root: Path | None = None,
        allowed_transports: frozenset[str] | None = None,
        catalog: MCPToolCatalog | None = None,
    ) -> None:
        if artifact_root is None or stderr_root is None:
            from assistant_agent.config.paths import state_paths

            paths = state_paths()
            artifact_root = artifact_root or paths.mcp_artifacts
            stderr_root = stderr_root or paths.mcp_stderr
        self._config = config
        self._logger = logger
        self._artifact_root = artifact_root.resolve()
        self._stderr_root = stderr_root.resolve()
        self._workspace_root = (workspace_root or Path.cwd()).resolve()
        self._run_control = run_control or RunControl()
        self._allowed_transports = (
            frozenset({"stdio", "http"}) if allowed_transports is None else allowed_transports
        )
        self._catalog = catalog
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: dict[str, _Server] = {}
        self._statuses: dict[str, MCPServerStatus] = {}
        self._status_lock = threading.RLock()
        self._background: Future | None = None
        self._lazy_configs: dict[str, MCPServerConfig] = {}
        self._transport_failures: dict[str, int] = {}
        self._breaker_until: dict[str, float] = {}
        self.warnings: list[str] = []

    _BREAKER_THRESHOLD = 3
    _BREAKER_COOLDOWN_SECONDS = 30.0

    # ---- 线程/loop ----

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """起守护线程跑常驻 event loop（幂等）。"""
        if self._loop is not None:
            return self._loop
        ready = threading.Event()
        loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_holder["loop"] = loop
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=_run, name="mcp-loop", daemon=True)
        self._thread.start()
        ready.wait()
        self._loop = loop_holder["loop"]
        return self._loop

    def _submit(self, coro: Any, timeout: float, *, respect_control: bool = True) -> Any:
        """把协程投进 loop 线程并同步等结果。超时则取消并抛 TimeoutError。"""
        loop = self._ensure_loop()
        fut: Future = asyncio.run_coroutine_threadsafe(coro, loop)
        deadline = time.monotonic() + timeout
        while True:
            state = self._run_control.state
            if respect_control and state.value > 0:
                fut.cancel()
                raise RunInterrupted(cancelled=self._run_control.cancel_requested)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fut.cancel()
                raise TimeoutError
            try:
                return fut.result(timeout=min(0.05, remaining))
            except FutureTimeoutError:
                continue

    # ---- 连接与发现 ----

    async def _open_transport(
        self, stack: AsyncExitStack, name: str, cfg: MCPServerConfig
    ) -> tuple:
        """按 type 分派 transport 工厂，返回 (read, write)。协议头/session 由 SDK 代管。

        stdio：stdio_client → 2 元组。
        http：streamablehttp_client → 3 元组（read, write, get_session_id），只取前两个。
        """
        return await open_transport(
            stack,
            name,
            cfg,
            artifact_root=self._artifact_root,
            stderr_root=self._stderr_root,
            workspace_root=self._workspace_root,
        )

    async def _connect_one(self, name: str, cfg: MCPServerConfig) -> _Server:
        """在 loop 线程里连接一个 server 并 initialize，持有上下文到 close。"""
        from mcp import ClientSession

        stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(stack, name, cfg)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return _Server(name=name, stack=stack, session=session)
        except BaseException:
            await stack.aclose()
            raise

    async def _connect_and_list(
        self,
        name: str,
        cfg: MCPServerConfig,
        semaphore: asyncio.Semaphore,
    ) -> _StartupResult:
        timeout = float(cfg.connect_timeout or cfg.timeout)
        async with semaphore:
            server: _Server | None = None
            try:
                server = await asyncio.wait_for(self._connect_one(name, cfg), timeout=timeout)
                listed = await asyncio.wait_for(server.session.list_tools(), timeout=timeout)
                return _StartupResult(server=server, listed=listed)
            except asyncio.CancelledError:
                if server is not None:
                    try:
                        await server.stack.aclose()
                    except Exception:
                        pass
                raise
            except TimeoutError:
                category = "timeout"
            except Exception:
                category = "discovery" if server is not None else "connection"
            if server is not None:
                try:
                    await server.stack.aclose()
                except Exception:
                    pass
            return _StartupResult(category=category)

    async def _start_parallel(
        self, servers: list[tuple[str, MCPServerConfig]]
    ) -> dict[str, _StartupResult]:
        semaphore = asyncio.Semaphore(self._config.connect_parallelism)
        results = await asyncio.gather(
            *(self._connect_and_list(name, cfg, semaphore) for name, cfg in servers)
        )
        return {name: result for (name, _cfg), result in zip(servers, results, strict=True)}

    def start(self) -> list[MCPTool]:
        """连接所有启用的 server，发现工具，返回过滤/限量后的 MCPTool 列表。

        单个 server 失败只跳过它（warnings 记录），不影响其余 server 与内置工具。
        """
        self.warnings = []
        self._statuses = {}
        now = datetime.now(UTC).isoformat()
        if not self._config.servers:
            return []
        if not self._config.enabled:
            self._statuses = {
                name: MCPServerStatus(name, cfg.type, cfg.startup, "disabled", checked_at=now)
                for name, cfg in self._config.servers.items()
            }
            return []
        allowed: list[tuple[str, MCPServerConfig]] = []
        required_failure: tuple[str, str] | None = None
        for name, cfg in self._config.servers.items():
            if not cfg.enabled:
                self._statuses[name] = MCPServerStatus(
                    name, cfg.type, cfg.startup, "disabled", checked_at=now
                )
            elif cfg.type not in self._allowed_transports:
                status = startup_failure_status(cfg.startup, "policy")
                self._statuses[name] = MCPServerStatus(
                    name,
                    cfg.type,
                    cfg.startup,
                    status,
                    checked_at=now,
                    error_category="policy",
                )
                self.warnings.append(f"MCP server {name} 被 Runtime policy 禁用")
                if cfg.startup == "required":
                    required_failure = (name, "policy")
            else:
                allowed.append((name, cfg))

        results: dict[str, _StartupResult] = {}
        if allowed:
            max_timeout = max(float(cfg.connect_timeout or cfg.timeout) for _, cfg in allowed)
            batches = (
                len(allowed) + self._config.connect_parallelism - 1
            ) // self._config.connect_parallelism
            results = self._submit(
                self._start_parallel(allowed),
                timeout=2 * max_timeout * batches + 2,
                respect_control=False,
            )
        tools: list[MCPTool] = []
        used_names: set[str] = set()
        for name, cfg in self._config.servers.items():
            result = results.get(name)
            if result is None:
                continue
            if result.server is None:
                category = result.category or "connection"
                status = startup_failure_status(cfg.startup, category)
                self._statuses[name] = MCPServerStatus(
                    name,
                    cfg.type,
                    cfg.startup,
                    status,
                    checked_at=now,
                    error_category=category,
                )
                self.warnings.append(f"MCP server {name} 启动失败，已安全降级（{category}）")
                if cfg.startup == "required":
                    required_failure = required_failure or (name, category)
                continue
            server = result.server
            self._servers[name] = server
            if self._catalog is not None:
                try:
                    self._catalog.save(name, cfg, definitions_from_listed(result.listed))
                except (OSError, ValueError) as exc:
                    self.warnings.append(f"MCP server {name} 工具目录写入失败：{exc}")
            discovered = self._build_discovered(
                name, cfg, server, result.listed, used_names, budget=len(tools)
            )
            tools.extend(discovered)
            self._statuses[name] = MCPServerStatus(
                name,
                cfg.type,
                cfg.startup,
                "connected",
                tool_names=tuple(server.tool_names),
                checked_at=now,
            )
        if required_failure is not None:
            self._close_servers()
            raise MCPRequiredServerError(*required_failure)
        return tools

    def start_runtime(self) -> list[MCPTool]:
        """准备 Runtime 工具；required 同步，optional 使用目录并按调用连接。"""
        self.warnings = []
        self._statuses = {}
        self._lazy_configs = {}
        now = datetime.now(UTC).isoformat()
        if not self._config.servers:
            return []
        if not self._config.enabled:
            self._statuses = {
                name: MCPServerStatus(name, cfg.type, cfg.startup, "disabled", checked_at=now)
                for name, cfg in self._config.servers.items()
            }
            return []

        required: list[tuple[str, MCPServerConfig]] = []
        optional: list[tuple[str, MCPServerConfig]] = []
        required_failure: tuple[str, str] | None = None
        for name, cfg in self._config.servers.items():
            if not cfg.enabled:
                self._statuses[name] = MCPServerStatus(
                    name, cfg.type, cfg.startup, "disabled", checked_at=now
                )
            elif cfg.type not in self._allowed_transports:
                status = startup_failure_status(cfg.startup, "policy")
                self._statuses[name] = MCPServerStatus(
                    name,
                    cfg.type,
                    cfg.startup,
                    status,
                    checked_at=now,
                    error_category="policy",
                )
                self.warnings.append(f"MCP server {name} 被 Runtime policy 禁用")
                if cfg.startup == "required":
                    required_failure = (name, "policy")
            elif cfg.startup == "required":
                required.append((name, cfg))
            else:
                optional.append((name, cfg))

        required_results = self._start_selected(required) if required else {}
        tools: list[MCPTool] = []
        used_names: set[str] = set()
        missing_catalogs: list[tuple[str, MCPServerConfig]] = []
        optional_names = {name for name, _ in optional}
        required_names = {name for name, _ in required}
        for name, cfg in self._config.servers.items():
            if name in required_names:
                result = required_results.get(name, _StartupResult(category="connection"))
                if result.server is None:
                    category = result.category or "connection"
                    self._statuses[name] = MCPServerStatus(
                        name,
                        cfg.type,
                        cfg.startup,
                        "required_failed",
                        checked_at=now,
                        error_category=category,
                    )
                    required_failure = required_failure or (name, category)
                    continue
                server = result.server
                self._servers[name] = server
                definitions = definitions_from_listed(result.listed)
                checked_at = now
                if self._catalog is not None:
                    try:
                        checked_at = self._catalog.save(name, cfg, definitions).checked_at
                    except (OSError, ValueError) as exc:
                        self.warnings.append(f"MCP server {name} 工具目录写入失败：{exc}")
                discovered = build_tools_from_definitions(
                    config=self._config,
                    name=name,
                    server_config=cfg,
                    definitions=definitions,
                    tool_names=server.tool_names,
                    used_names=used_names,
                    budget=len(tools),
                    warnings=self.warnings,
                    caller=self._call_tool,
                )
                tools.extend(discovered)
                self._statuses[name] = MCPServerStatus(
                    name,
                    cfg.type,
                    cfg.startup,
                    "connected",
                    tool_names=tuple(server.tool_names),
                    checked_at=checked_at,
                )
            elif name in optional_names:
                snapshot = self._catalog.load(name, cfg) if self._catalog is not None else None
                if snapshot is None:
                    self._statuses[name] = MCPServerStatus(
                        name, cfg.type, cfg.startup, "discovering", checked_at=now
                    )
                    missing_catalogs.append((name, cfg))
                    continue
                tool_names: list[str] = []
                discovered = build_tools_from_definitions(
                    config=self._config,
                    name=name,
                    server_config=cfg,
                    definitions=snapshot.definitions,
                    tool_names=tool_names,
                    used_names=used_names,
                    budget=len(tools),
                    warnings=self.warnings,
                    caller=self._call_tool,
                )
                tools.extend(discovered)
                self._lazy_configs[name] = cfg
                self._statuses[name] = MCPServerStatus(
                    name,
                    cfg.type,
                    cfg.startup,
                    "available_cached",
                    tool_names=tuple(tool_names),
                    checked_at=snapshot.checked_at,
                )

        if required_failure is not None:
            self._close_servers()
            raise MCPRequiredServerError(*required_failure)
        if missing_catalogs:
            self._schedule_catalog_discovery(missing_catalogs)
        return tools

    def _start_selected(
        self, servers: list[tuple[str, MCPServerConfig]]
    ) -> dict[str, _StartupResult]:
        max_timeout = max(float(cfg.connect_timeout or cfg.timeout) for _, cfg in servers)
        batches = (
            len(servers) + self._config.connect_parallelism - 1
        ) // self._config.connect_parallelism
        return self._submit(
            self._start_parallel(servers),
            timeout=2 * max_timeout * batches + 2,
            respect_control=False,
        )

    def _schedule_catalog_discovery(self, servers: list[tuple[str, MCPServerConfig]]) -> None:
        loop = self._ensure_loop()
        coroutine = self._discover_catalogs(servers)
        try:
            self._background = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except BaseException:
            coroutine.close()
            raise

    async def _discover_catalogs(self, servers: list[tuple[str, MCPServerConfig]]) -> None:
        semaphore = asyncio.Semaphore(self._config.connect_parallelism)
        results = await asyncio.gather(
            *(self._connect_and_list(name, cfg, semaphore) for name, cfg in servers)
        )
        for (name, cfg), result in zip(servers, results, strict=True):
            now = datetime.now(UTC).isoformat()
            if result.server is None:
                category = result.category or "connection"
                with self._status_lock:
                    self._statuses[name] = MCPServerStatus(
                        name,
                        cfg.type,
                        cfg.startup,
                        startup_failure_status(cfg.startup, category),
                        checked_at=now,
                        error_category=category,
                    )
                continue
            try:
                definitions = definitions_from_listed(result.listed)
                checked_at = now
                if self._catalog is not None:
                    checked_at = self._catalog.save(name, cfg, definitions).checked_at
                with self._status_lock:
                    self._statuses[name] = MCPServerStatus(
                        name,
                        cfg.type,
                        cfg.startup,
                        "restart_required",
                        tool_names=tuple(item.raw_name for item in definitions),
                        checked_at=checked_at,
                    )
            except (OSError, ValueError):
                with self._status_lock:
                    self._statuses[name] = MCPServerStatus(
                        name,
                        cfg.type,
                        cfg.startup,
                        "degraded_discovery",
                        checked_at=now,
                        error_category="catalog",
                    )
            finally:
                try:
                    await result.server.stack.aclose()
                except Exception:
                    pass

    def close(self) -> None:
        """关闭所有 session/子进程并停 loop。幂等。"""
        if self._loop is None:
            return
        if self._background is not None:
            if not self._background.done():
                self._background.cancel()
            try:
                self._background.result(timeout=5)
            except BaseException:
                pass
            # concurrent Future 会先进入 cancelled，底层 asyncio Task 仍需一次 loop 周期
            # 才能处理取消并关闭尚未启动/正在连接的 transport。
            try:
                self._submit(asyncio.sleep(0), timeout=5, respect_control=False)
            except Exception:
                pass
        self._close_servers()
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._background = None

    def _close_servers(self) -> None:
        for server in self._servers.values():
            try:
                self._submit(server.stack.aclose(), timeout=10, respect_control=False)
            except Exception:  # 关闭尽力而为，不抛
                pass
        self._servers.clear()

    def _discover(
        self,
        name: str,
        cfg: MCPServerConfig,
        server: _Server,
        used_names: set[str],
        budget: int,
    ) -> list[MCPTool]:
        """列工具，套白/黑名单 + 每 server/全局上限 + 名称规范化/碰撞，建 MCPTool。"""
        try:
            listed = self._submit(
                server.session.list_tools(), timeout=float(cfg.connect_timeout or cfg.timeout)
            )
        except Exception as exc:
            self.warnings.append(f"MCP server {name} 列工具失败，已跳过：{exc}")
            return []
        return self._build_discovered(name, cfg, server, listed, used_names, budget)

    def _build_discovered(
        self,
        name: str,
        cfg: MCPServerConfig,
        server: _Server,
        listed: Any,
        used_names: set[str],
        budget: int,
    ) -> list[MCPTool]:
        return build_discovered_tools(
            config=self._config,
            name=name,
            server_config=cfg,
            server=server,
            listed=listed,
            used_names=used_names,
            budget=budget,
            warnings=self.warnings,
            caller=self._call_tool,
        )

    def _call_tool(
        self,
        server: str,
        raw_tool: str,
        args: dict[str, Any],
        timeout: float,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        """MCPTool.run 的同步桥入口：把 call_tool 投进 loop 线程等结果。"""
        now = time.monotonic()
        with self._status_lock:
            breaker_until = self._breaker_until.get(server, 0.0)
            if breaker_until > now:
                raise MCPDependencyUnavailable(f"MCP server {server} 已进入短时熔断")
            self._breaker_until.pop(server, None)
        if server not in self._servers:
            self._connect_lazy(server)
        connected = self._servers[server]
        if connected.tool_names and raw_tool not in connected.tool_names:
            raise MCPDependencyUnavailable(f"MCP server {server} 不再提供工具 {raw_tool}")
        session = connected.session
        try:
            result = self._submit(
                session.call_tool(raw_tool, args, meta=meta or None), timeout=timeout
            )
        except (TimeoutError, OSError, ConnectionError):
            self._record_transport_failure(server)
            raise
        except Exception:
            # SDK/transport 异常没有稳定的公共类型；这条路径仅记录熔断计数，
            # MCPTool 仍负责把它转换为安全的调用结果。
            self._record_transport_failure(server)
            raise
        self._record_transport_success(server)
        return result

    def _record_transport_failure(self, server: str) -> None:
        with self._status_lock:
            failures = self._transport_failures.get(server, 0) + 1
            self._transport_failures[server] = failures
            if failures < self._BREAKER_THRESHOLD:
                return
            self._breaker_until[server] = time.monotonic() + self._BREAKER_COOLDOWN_SECONDS
            status = self._statuses.get(server)
            if status is not None:
                self._statuses[server] = MCPServerStatus(
                    status.name,
                    status.transport,
                    status.startup,
                    "degraded_connection",
                    tool_names=status.tool_names,
                    checked_at=datetime.now(UTC).isoformat(),
                    error_category="breaker",
                )

    def _record_transport_success(self, server: str) -> None:
        with self._status_lock:
            self._transport_failures.pop(server, None)
            self._breaker_until.pop(server, None)

    def _connect_lazy(self, name: str) -> None:
        cfg = self._lazy_configs.get(name)
        if cfg is None:
            raise RuntimeError(f"MCP server {name} 当前不可用")
        now = datetime.now(UTC).isoformat()
        with self._status_lock:
            previous = self._statuses.get(name)
            self._statuses[name] = MCPServerStatus(
                name,
                cfg.type,
                cfg.startup,
                "connecting",
                tool_names=previous.tool_names if previous is not None else (),
                checked_at=now,
            )
        try:
            result = self._submit(
                self._connect_and_list(name, cfg, asyncio.Semaphore(1)),
                timeout=2 * float(cfg.connect_timeout or cfg.timeout) + 2,
            )
        except TimeoutError as exc:
            with self._status_lock:
                self._statuses[name] = MCPServerStatus(
                    name,
                    cfg.type,
                    cfg.startup,
                    "degraded_timeout",
                    checked_at=now,
                    error_category="timeout",
                )
            raise MCPDependencyUnavailable(f"MCP server {name} 连接超时") from exc
        if result.server is None:
            category = result.category or "connection"
            with self._status_lock:
                self._statuses[name] = MCPServerStatus(
                    name,
                    cfg.type,
                    cfg.startup,
                    startup_failure_status(cfg.startup, category),
                    checked_at=now,
                    error_category=category,
                )
            if category == "timeout":
                raise MCPDependencyUnavailable(f"MCP server {name} 连接超时")
            raise MCPDependencyUnavailable(f"MCP server {name} 连接失败")
        server = result.server
        definitions = definitions_from_listed(result.listed)
        server.tool_names = [item.raw_name for item in definitions]
        self._servers[name] = server
        visible_tool_names = previous.tool_names if previous is not None else ()
        checked_at = now
        if self._catalog is not None:
            try:
                checked_at = self._catalog.save(name, cfg, definitions).checked_at
            except (OSError, ValueError):
                pass
        with self._status_lock:
            self._statuses[name] = MCPServerStatus(
                name,
                cfg.type,
                cfg.startup,
                "connected",
                tool_names=visible_tool_names,
                checked_at=checked_at,
            )

    def server_summary(self) -> list[tuple[str, list[str]]]:
        """(server 名, 其原始工具名列表) 列表，供 /mcp 展示。"""
        with self._status_lock:
            return [
                (name, list(status.tool_names))
                for name, status in self._statuses.items()
                if status.tool_names
                and (
                    name in self._servers
                    or name in self._lazy_configs
                    or status.status == "restart_required"
                )
            ]

    def restrict_optional_runtime_tools(self, tools: list[MCPTool]) -> None:
        """让 capability 只展示当前 Registry 实际注册的 optional 工具。"""
        allowed: dict[str, set[str]] = {}
        for tool in tools:
            allowed.setdefault(tool.server_name, set()).add(tool.raw_name)
        with self._status_lock:
            for name, status in tuple(self._statuses.items()):
                cfg = self._config.servers.get(name)
                if cfg is None or cfg.startup != "optional" or status.status == "restart_required":
                    continue
                names = allowed.get(name, set())
                self._statuses[name] = MCPServerStatus(
                    status.name,
                    status.transport,
                    status.startup,
                    status.status,
                    tool_names=tuple(item for item in status.tool_names if item in names),
                    checked_at=status.checked_at,
                    error_category=status.error_category,
                )

    def server_statuses(self) -> tuple[MCPServerStatus, ...]:
        with self._status_lock:
            return tuple(self._statuses.values())

    def server_capabilities(self) -> tuple[MCPServerCapability, ...]:
        return tuple(
            MCPServerCapability(
                name=item.name,
                transport=item.transport,
                startup=item.startup,
                status=item.status,
                tool_names=item.tool_names,
                checked_at=item.checked_at,
                error_category=item.error_category,
            )
            for item in self.server_statuses()
        )
