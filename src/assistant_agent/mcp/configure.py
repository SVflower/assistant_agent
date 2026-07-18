"""MCP server 配置、隔离探测与受管清单。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from assistant_agent.config.paths import managed_mcp_dir, state_paths, workspace_id
from assistant_agent.config.schema import MCPConfig, MCPServerConfig
from assistant_agent.config.writer import ConfigScope, MCPConfigStore
from assistant_agent.mcp.manager import MCPManager
from assistant_agent.mcp.status import MCPRequiredServerError
from assistant_agent.obs import NullLogger

_ENV_REFERENCE = re.compile(r"^(?:[^$]*\$\{[A-Za-z_][A-Za-z0-9_]*\}[^$]*)+$")
_SECRET_LIKE = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|gh[ps]_[a-z0-9]{12,}|bearer\s+[^$\s]+)")
_SECRET_KEY = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|authorization|cookie|credential)"
)


class MCPConfigureError(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPProbeResult:
    server: str
    tools: tuple[str, ...]
    warnings: tuple[str, ...]


class MCPService:
    """控制 MCP 配置生命周期；不会修改当前 Runtime 的 registry。"""

    def __init__(
        self,
        project_config: Path,
        logger: NullLogger | None = None,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        self.store = MCPConfigStore(project_config)
        self.logger = logger or NullLogger()
        self.workspace_root = (workspace_root or self.store.project_config.parent).resolve()

    def list(self) -> dict[str, tuple[ConfigScope, MCPServerConfig]]:
        return self.store.list_scoped()

    def probe(self, name: str, server: MCPServerConfig) -> MCPProbeResult:
        _validate_secret_references(server)
        with tempfile.TemporaryDirectory(prefix="assistant-agent-mcp-probe-") as temp:
            root = Path(temp)
            manager = MCPManager(
                MCPConfig(servers={name: server}),
                self.logger,
                artifact_root=root / "artifacts",
                stderr_root=root / "stderr",
            )
            try:
                try:
                    tools = manager.start()
                except MCPRequiredServerError as exc:
                    raise MCPConfigureError(str(exc)) from exc
                warnings = tuple(manager.warnings)
                if warnings and not tools:
                    raise MCPConfigureError(warnings[0])
                names = tuple(tool.name for tool in tools)
                return MCPProbeResult(name, names, warnings)
            finally:
                manager.close()

    def add(
        self,
        name: str,
        server: MCPServerConfig,
        scope: ConfigScope = "user",
        *,
        verify: bool = True,
    ) -> MCPProbeResult:
        _validate_secret_references(server)
        result = self.probe(name, server) if verify else MCPProbeResult(name, (), ())
        manifest_path = self._manifest_path(name, scope)
        old_manifest = _read_manifest_bytes(manifest_path)
        _write_manifest(manifest_path, name, scope, server)
        try:
            self.store.add(name, server, scope)
        except BaseException:
            _restore_manifest(manifest_path, old_manifest)
            raise
        return result

    def remove(self, name: str, scope: ConfigScope = "user") -> bool:
        removed = self.store.remove(name, scope)
        if not removed:
            return False
        manifest_path = self._manifest_path(name, scope)
        manifest = _read_manifest(manifest_path)
        if manifest and manifest.get("name") == name and manifest.get("scope") == scope:
            manifest_path.unlink()
            try:
                manifest_path.parent.rmdir()
            except OSError:
                pass
        return True

    def set_enabled(self, name: str, enabled: bool, scope: ConfigScope) -> MCPServerConfig:
        server = self._require(name, scope)
        updated = server.model_copy(update={"enabled": enabled})
        self.store.add(name, updated, scope)
        return updated

    def set_trusted(self, name: str, trusted: bool, scope: ConfigScope) -> MCPServerConfig:
        server = self._require(name, scope)
        updated = server.model_copy(update={"auto_approve": trusted})
        self.store.add(name, updated, scope)
        return updated

    def purge_artifacts(self, name: str) -> bool:
        """只清理当前项目、指定 server 的受管历史 artifact。"""
        self.store.validate_name(name)
        root = state_paths(self.workspace_root).mcp_artifacts.resolve()
        target = (root / name).resolve()
        if target.parent != root:
            raise MCPConfigureError("MCP artifact 清理路径逃逸")
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

    def _require(self, name: str, scope: ConfigScope) -> MCPServerConfig:
        server = self.store.get(name, scope)
        if server is None:
            raise MCPConfigureError(f"{scope} scope 中不存在 MCP server：{name}")
        return server

    def _manifest_path(self, name: str, scope: ConfigScope) -> Path:
        suffix = "user" if scope == "user" else workspace_id(self.workspace_root)
        return managed_mcp_dir() / name / f"{suffix}.json"


def _validate_secret_references(server: MCPServerConfig) -> None:
    for key, value in server.env.items():
        if not value or _ENV_REFERENCE.fullmatch(value):
            continue
        if _SECRET_KEY.search(key) or _SECRET_LIKE.search(value):
            raise MCPConfigureError(f"{key} 疑似密钥，必须使用 ${{ENV_VAR}} 引用，不能写入明文值")
    for key, value in server.headers.items():
        if value and not _ENV_REFERENCE.fullmatch(value):
            raise MCPConfigureError(f"HTTP header {key} 必须使用 ${{ENV_VAR}} 引用，不能写入明文值")
    values = [server.command, server.url, *server.args]
    if any(_SECRET_LIKE.search(value) for value in values):
        raise MCPConfigureError("command/args/url 中疑似包含明文密钥")


def _write_manifest(path: Path, name: str, scope: ConfigScope, server: MCPServerConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    payload = {
        "version": 1,
        "name": name,
        "scope": scope,
        "config": server.model_dump(mode="json", exclude_defaults=True),
    }
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_manifest(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_manifest_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore_manifest(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
