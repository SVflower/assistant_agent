"""保留 YAML 注释的 MCP 配置事务写入。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from ruamel.yaml import YAML

from assistant_agent.config.paths import assistant_home
from assistant_agent.config.schema import AppConfig, MCPServerConfig

ConfigScope = Literal["user", "project"]


class ConfigWriteError(RuntimeError):
    pass


class MCPConfigStore:
    """user/project 两个 scope 的 MCP server 定义存取。"""

    def __init__(self, project_config: Path) -> None:
        self.project_config = project_config.resolve()
        self.user_config = assistant_home() / "mcp" / "servers.yaml"
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.indent(mapping=2, sequence=4, offset=2)

    def list_scoped(self) -> dict[str, tuple[ConfigScope, MCPServerConfig]]:
        merged: dict[str, tuple[ConfigScope, MCPServerConfig]] = {}
        for scope in ("user", "project"):
            raw = self._read_servers(scope)
            for name, value in raw.items():
                try:
                    merged[str(name)] = (scope, MCPServerConfig.model_validate(value))
                except ValidationError:
                    continue
        return merged

    def path(self, scope: ConfigScope) -> Path:
        """返回 scope 的配置文件位置，供权限预览展示。"""
        return self._path(scope)

    def add(self, name: str, server: MCPServerConfig, scope: ConfigScope) -> None:
        self.validate_name(name)
        document = self._read_document(scope)
        servers = self._servers_node(document, scope)
        servers[name] = server.model_dump(mode="json", exclude_defaults=True)
        self._validate_document(document, scope)
        self._atomic_dump(self._path(scope), document)

    def remove(self, name: str, scope: ConfigScope) -> bool:
        self.validate_name(name)
        document = self._read_document(scope)
        servers = self._servers_node(document, scope)
        if name not in servers:
            return False
        del servers[name]
        self._validate_document(document, scope)
        self._atomic_dump(self._path(scope), document)
        return True

    def get(self, name: str, scope: ConfigScope | None = None) -> MCPServerConfig | None:
        if scope is not None:
            value = self._read_servers(scope).get(name)
            return MCPServerConfig.model_validate(value) if value is not None else None
        item = self.list_scoped().get(name)
        return item[1] if item else None

    def _path(self, scope: ConfigScope) -> Path:
        return self.user_config if scope == "user" else self.project_config

    def _read_document(self, scope: ConfigScope) -> Any:
        path = self._path(scope)
        if not path.is_file():
            return {} if scope == "project" else {"servers": {}}
        try:
            data = self._yaml.load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigWriteError(f"无法读取配置 {path}：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigWriteError(f"配置根节点必须是映射：{path}")
        return data

    def _read_servers(self, scope: ConfigScope) -> dict[str, Any]:
        document = self._read_document(scope)
        node = (
            document.get("servers", {})
            if scope == "user"
            else document.get("mcp", {}).get("servers", {})
        )
        return dict(node) if isinstance(node, dict) else {}

    @staticmethod
    def _servers_node(document: Any, scope: ConfigScope) -> Any:
        if scope == "user":
            return document.setdefault("servers", {})
        return document.setdefault("mcp", {}).setdefault("servers", {})

    @staticmethod
    def validate_name(name: str) -> None:
        if not name or len(name) > 64 or not all(ch.isalnum() or ch in "_-" for ch in name):
            raise ConfigWriteError("MCP server 名仅允许字母、数字、下划线和连字符（最长 64）")

    @staticmethod
    def _validate_document(document: Any, scope: ConfigScope) -> None:
        try:
            if scope == "project":
                AppConfig.model_validate(document)
            else:
                servers = document.get("servers", {})
                if not isinstance(servers, dict):
                    raise ConfigWriteError("user MCP servers 必须是映射")
                for value in servers.values():
                    MCPServerConfig.model_validate(value)
        except ValidationError as exc:
            raise ConfigWriteError(f"候选配置校验失败：{exc}") from exc

    def _atomic_dump(self, path: Path, document: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                self._yaml.dump(document, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
