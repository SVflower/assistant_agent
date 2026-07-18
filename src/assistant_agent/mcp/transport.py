"""MCP transport 创建与最小子进程环境。"""

from __future__ import annotations

import os
import re
from contextlib import AsyncExitStack
from pathlib import Path

from assistant_agent.config.schema import MCPServerConfig
from assistant_agent.mcp.discovery import _sanitize

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
    errlog = stack.enter_context(
        (stderr_dir / "server.log").open("a", encoding="utf-8", errors="replace")
    )
    read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
    return read, write
