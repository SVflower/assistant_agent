"""MCPManager：连接 MCP server（stdio）、发现工具、同步桥、生命周期。

同步/异步桥（最大的坎）：mcp SDK 是 asyncio，我们的 Tool.run() 是同步。
方案：起一个守护线程跑常驻 event loop，连接/持有 ClientSession 都在该 loop 里；
MCPTool.run() 用 run_coroutine_threadsafe 把协程投进去、同步等结果。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from assistant_agent.mcp.tool import MCPTool
from assistant_agent.runtime import RunControl, RunInterrupted
from assistant_agent.tools.validation import ToolSchemaError, build_validator

if TYPE_CHECKING:
    from assistant_agent.config.schema import MCPConfig, MCPServerConfig
    from assistant_agent.obs import NullLogger

_NAME_SANITIZE = re.compile(r"[^a-zA-Z0-9_]")
_ENV_REF = re.compile(r"\$\{([^}]+)\}")
_BASE_ENV_KEYS = {
    "COMSPEC",
    "HOME",
    "LANG",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


def _sanitize(part: str) -> str:
    """把 server/tool 名规范成 [a-zA-Z0-9_]，供拼装注册名。"""
    return _NAME_SANITIZE.sub("_", part)


def _interpolate_env(env: dict[str, str]) -> dict[str, str]:
    """把值里的 ${VAR} 从进程环境替换（可嵌在串中，如 'Bearer ${TOK}'）；缺失留空串。

    密钥不落配置：配 ${TOKEN}，真值从环境取。
    """
    return {
        key: _ENV_REF.sub(lambda mo: os.environ.get(mo.group(1), ""), value)
        for key, value in env.items()
    }


def _minimal_env() -> dict[str, str]:
    """保留进程启动所需基础变量，不继承 token/secret/key 等宿主凭据。"""
    return {key: value for key, value in os.environ.items() if key.upper() in _BASE_ENV_KEYS}


@dataclass
class _Server:
    """一个已连接 server 的运行态。"""

    name: str
    stack: AsyncExitStack
    session: Any
    tool_names: list[str] = field(default_factory=list)


class MCPManager:
    """管理所有 MCP server 的生命周期与工具桥接。

    用法：m = MCPManager(config, logger); tools = m.start(); ... ; m.close()
    start() 返回发现并通过过滤/上限的 MCPTool 列表，供 main 注册进 registry。
    """

    def __init__(
        self,
        config: MCPConfig,
        logger: NullLogger,
        *,
        artifact_root: Path | None = None,
        stderr_root: Path | None = None,
        run_control: RunControl | None = None,
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
        self._run_control = run_control or RunControl()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: dict[str, _Server] = {}
        self.warnings: list[str] = []

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
        if cfg.type == "http":
            from mcp.client.streamable_http import streamablehttp_client

            streams = await stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=_interpolate_env(cfg.headers) or None)
            )
            return streams[0], streams[1]  # 丢弃 get_session_id：session 归 SDK 管
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        artifact_dir = (self._artifact_root / _sanitize(name)).resolve()
        stderr_dir = (self._stderr_root / _sanitize(name)).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stderr_dir.mkdir(parents=True, exist_ok=True)
        cwd = Path(cfg.cwd).expanduser().resolve() if cfg.cwd else artifact_dir
        env = {**_minimal_env(), **_interpolate_env(cfg.env)}
        env.setdefault("ASSISTANT_AGENT_ARTIFACT_DIR", str(artifact_dir))
        args = _managed_args(cfg.command, list(cfg.args), artifact_dir)
        params = StdioServerParameters(
            command=cfg.command,
            args=args,
            env=env,
            cwd=cwd,
        )
        errlog = stack.enter_context(
            (stderr_dir / "server.log").open("a", encoding="utf-8", errors="replace")
        )
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        return read, write

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

    def start(self) -> list[MCPTool]:
        """连接所有启用的 server，发现工具，返回过滤/限量后的 MCPTool 列表。

        单个 server 失败只跳过它（warnings 记录），不影响其余 server 与内置工具。
        """
        self.warnings = []
        if not self._config.enabled or not self._config.servers:
            return []
        tools: list[MCPTool] = []
        used_names: set[str] = set()
        for name, cfg in self._config.servers.items():
            if not cfg.enabled:
                continue
            if len(tools) >= self._config.max_total_tools:
                self.warnings.append(
                    f"已达全局工具上限 {self._config.max_total_tools}，跳过 {name}"
                )
                break
            try:
                server = self._submit(self._connect_one(name, cfg), timeout=cfg.timeout)
            except Exception as exc:  # 连接/握手失败 → 跳过该 server
                self.warnings.append(f"MCP server {name} 连接失败，已跳过：{exc}")
                continue
            self._servers[name] = server
            tools.extend(self._discover(name, cfg, server, used_names, budget=len(tools)))
        return tools

    def close(self) -> None:
        """关闭所有 session/子进程并停 loop。幂等。"""
        if self._loop is None:
            return
        for server in self._servers.values():
            try:
                self._submit(server.stack.aclose(), timeout=10, respect_control=False)
            except Exception:  # 关闭尽力而为，不抛
                pass
        self._servers.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None

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
            listed = self._submit(server.session.list_tools(), timeout=cfg.timeout)
        except Exception as exc:
            self.warnings.append(f"MCP server {name} 列工具失败，已跳过：{exc}")
            return []
        out: list[MCPTool] = []
        server_slug = _sanitize(name)
        include = set(cfg.include_tools)
        exclude = set(cfg.exclude_tools)
        for raw in getattr(listed, "tools", None) or []:
            raw_name = raw.name
            if include and raw_name not in include:
                continue
            if raw_name in exclude:
                continue
            input_schema = raw.inputSchema or {"type": "object", "properties": {}}
            output_schema = getattr(raw, "outputSchema", None)
            try:
                build_validator(f"mcp__{name}__{raw_name}", input_schema)
                if output_schema is not None:
                    build_validator(f"mcp__{name}__{raw_name} output", output_schema)
            except ToolSchemaError as exc:
                self.warnings.append(f"MCP 工具 {name}/{raw_name} schema 无效，已跳过：{exc}")
                continue
            if len(out) >= cfg.max_tools:
                self.warnings.append(f"server {name} 达工具上限 {cfg.max_tools}，其余丢弃")
                break
            if budget + len(out) >= self._config.max_total_tools:
                self.warnings.append(
                    f"达全局工具上限 {self._config.max_total_tools}，{name} 部分工具丢弃"
                )
                break
            registered = f"mcp__{server_slug}__{_sanitize(raw_name)}"
            if registered in used_names:  # 规范化后碰撞 → 加序号，绝不静默覆盖
                suffix = 2
                while f"{registered}_{suffix}" in used_names:
                    suffix += 1
                registered = f"{registered}_{suffix}"
            used_names.add(registered)
            server.tool_names.append(raw_name)
            annotations = _tool_annotations(getattr(raw, "annotations", None))
            policy = cfg.tool_policies.get(raw_name)
            replay = policy.replay if policy is not None else "default"
            destructive = annotations.get("destructiveHint") is True
            trusted_readonly = replay == "safe_readonly" or (
                replay == "default"
                and cfg.trust_tool_annotations
                and annotations.get("readOnlyHint") is True
                and not destructive
            )
            if replay == "requires_decision" or destructive:
                trusted_readonly = False
            outcome_unknown = not trusted_readonly
            if policy is not None and policy.outcome_on_transport_error == "unknown":
                outcome_unknown = True
            timeout = float(
                policy.timeout if policy is not None and policy.timeout else cfg.timeout
            )
            out.append(
                MCPTool(
                    server=name,
                    registered_name=registered,
                    raw_tool=raw_name,
                    description=raw.description or "",
                    input_schema=input_schema,
                    caller=self._call_tool,
                    timeout=timeout,
                    auto_approve=cfg.auto_approve and not destructive,
                    output_schema=output_schema,
                    annotations=annotations,
                    trusted_readonly=trusted_readonly,
                    outcome_unknown_on_transport_error=outcome_unknown,
                )
            )
        return out

    def _call_tool(
        self,
        server: str,
        raw_tool: str,
        args: dict[str, Any],
        timeout: float,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        """MCPTool.run 的同步桥入口：把 call_tool 投进 loop 线程等结果。"""
        session = self._servers[server].session
        return self._submit(session.call_tool(raw_tool, args, meta=meta or None), timeout=timeout)

    def server_summary(self) -> list[tuple[str, list[str]]]:
        """(server 名, 其原始工具名列表) 列表，供 /mcp 展示。"""
        return [(name, list(s.tool_names)) for name, s in self._servers.items()]


def _managed_args(command: str, args: list[str], artifact_dir: Path) -> list[str]:
    """为已知 MCP 注入官方产物目录参数；用户显式参数优先。"""
    joined = " ".join([command, *args]).lower()
    if "@playwright/mcp" not in joined or "--output-dir" in args:
        return args
    return [
        *args,
        "--output-dir",
        str(artifact_dir),
        "--output-max-size",
        str(100 * 1024 * 1024),
    ]


def _tool_annotations(value: Any) -> dict[str, Any]:
    """兼容 SDK Pydantic 模型与测试字典，只保留稳定的 MCP annotation 字段。"""
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        raw = value.model_dump(by_alias=True, exclude_none=True)
    elif isinstance(value, dict):
        raw = value
    else:
        raw = {
            key: getattr(value, key)
            for key in (
                "title",
                "readOnlyHint",
                "destructiveHint",
                "idempotentHint",
                "openWorldHint",
            )
            if getattr(value, key, None) is not None
        }
    allowed = {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    return {key: raw[key] for key in allowed if key in raw}
