"""用户级安装目录与按工作区隔离的运行状态路径。"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
LEGACY_RUN_DIR = Path(".assistant_agent") / "runs"
LEGACY_LOG_DIR = Path(".assistant_agent") / "logs"


@dataclass(frozen=True)
class StatePaths:
    home: Path
    workspace: Path
    sessions: Path
    runs: Path
    logs: Path
    tool_artifacts: Path
    mcp_artifacts: Path
    mcp_stderr: Path
    mcp_catalog: Path
    attachments: Path


def assistant_home() -> Path:
    override = os.environ.get("ASSISTANT_AGENT_HOME", "").strip()
    return (Path(override).expanduser() if override else Path.home() / ".assistant_agent").resolve()


def workspace_id(workspace_root: Path) -> str:
    root = workspace_root.expanduser().resolve()
    normalized = os.path.normcase(str(root))
    digest = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    readable = _SAFE_NAME.sub("-", root.name).strip("-.")[:40] or "workspace"
    return f"{readable}-{digest}"


def state_paths(workspace_root: Path | None = None) -> StatePaths:
    home = assistant_home()
    root = (workspace_root or Path.cwd()).resolve()
    workspace = home / "workspaces" / workspace_id(root)
    artifacts = workspace / "artifacts"
    return StatePaths(
        home=home,
        workspace=workspace,
        sessions=workspace / "sessions",
        runs=workspace / "runs",
        logs=workspace / "logs",
        tool_artifacts=artifacts / "tools",
        mcp_artifacts=artifacts / "mcp",
        mcp_stderr=workspace / "mcp-stderr",
        mcp_catalog=workspace / "cache" / "mcp-tools",
        attachments=workspace / "attachments",
    )


def user_skills_dir() -> Path:
    return assistant_home() / "skills"


def managed_mcp_dir() -> Path:
    return assistant_home() / "mcp" / "servers"


def project_skills_dir(workspace_root: Path | None = None) -> Path:
    return (workspace_root or Path.cwd()).resolve() / "skills"


def resolve_run_dir(configured: str, workspace_root: Path | None = None) -> Path:
    path = Path(configured).expanduser()
    if path == LEGACY_RUN_DIR:
        return state_paths(workspace_root).runs
    root = (workspace_root or Path.cwd()).resolve()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def resolve_log_dir(configured: str, workspace_root: Path | None = None) -> Path:
    path = Path(configured).expanduser()
    if path == LEGACY_LOG_DIR:
        return state_paths(workspace_root).logs
    root = (workspace_root or Path.cwd()).resolve()
    return path.resolve() if path.is_absolute() else (root / path).resolve()
