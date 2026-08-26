"""MCP transport 创建与最小子进程环境。"""

from __future__ import annotations

import os
import re
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TextIO, cast

from assistant_agent.config.schema import MCPServerConfig
from assistant_agent.integrations.mcp.discovery import _sanitize

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
_STDERR_MAX_BYTES = 256 * 1024


class _BoundedStderr:
    """把 MCP 子进程 stderr drain 到固定大小的尾部诊断文件。"""

    def __init__(self, path: Path, *, max_bytes: int = _STDERR_MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._path.touch()
        self._read_fd, self.write_fd = os.pipe()
        self._done = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._drain, name="mcp-stderr", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        tail = bytearray()
        try:
            with os.fdopen(self._read_fd, "rb", closefd=True) as source:
                while True:
                    chunk = source.read(8192)
                    if not chunk:
                        break
                    tail.extend(chunk)
                    if len(tail) > self._max_bytes:
                        del tail[: len(tail) - self._max_bytes]
                    self._path.write_bytes(tail)
        finally:
            self._done.set()

    def close_write_end(self) -> None:
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_write_end()
        if not self._done.wait(timeout=5):
            try:
                os.close(self._read_fd)
            except OSError:
                pass
        self._thread.join(timeout=1)


def _interpolate_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: _ENV_REF.sub(lambda match: os.environ.get(match.group(1), ""), value)
        for key, value in env.items()
    }


def _minimal_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() in _BASE_ENV_KEYS}


def _managed_args(command: str, args: list[str], artifact_dir: Path) -> list[str]:
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


async def open_transport(
    stack: AsyncExitStack,
    name: str,
    cfg: MCPServerConfig,
    *,
    artifact_root: Path,
    stderr_root: Path,
    workspace_root: Path,
) -> tuple:
    if cfg.type == "http":
        from mcp.client.streamable_http import streamablehttp_client

        streams = await stack.enter_async_context(
            streamablehttp_client(cfg.url, headers=_interpolate_env(cfg.headers) or None)
        )
        return streams[0], streams[1]

    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    artifact_dir = (artifact_root / _sanitize(name)).resolve()
    stderr_dir = (stderr_root / _sanitize(name)).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stderr_dir.mkdir(parents=True, exist_ok=True)
    if cfg.cwd:
        configured_cwd = Path(cfg.cwd).expanduser()
        cwd = (
            configured_cwd.resolve()
            if configured_cwd.is_absolute()
            else (workspace_root / configured_cwd).resolve()
        )
    else:
        cwd = artifact_dir
    env = {**_minimal_env(), **_interpolate_env(cfg.env)}
    env.setdefault("ASSISTANT_AGENT_ARTIFACT_DIR", str(artifact_dir))
    params = StdioServerParameters(
        command=cfg.command,
        args=_managed_args(cfg.command, list(cfg.args), artifact_dir),
        env=env,
        cwd=cwd,
    )
    stderr_capture = _BoundedStderr(stderr_dir / "server.log")
    # 先注册清理，再进入 SDK context，保证 SDK 结束子进程后才关闭 drain 线程。
    stack.callback(stderr_capture.close)
    try:
        read, write = await stack.enter_async_context(
            # MCP SDK 的类型标注只写了 TextIO；anyio 最终接受同样合法的 stderr fd。
            stdio_client(params, errlog=cast(TextIO, stderr_capture.write_fd))
        )
    finally:
        # 子进程继承了 write fd；父进程必须立即关闭自己的副本，否则 EOF 永远不会到达。
        stderr_capture.close_write_end()
    return read, write
