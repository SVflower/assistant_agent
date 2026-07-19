"""MCP 工具目录缓存；只保存脱敏 Schema，不保存连接凭据或调用数据。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from assistant_agent.config.schema import MCPServerConfig
from assistant_agent.integrations.mcp.models import MCPToolDefinition

_SCHEMA_VERSION = 1
_MAX_CATALOG_BYTES = 2_000_000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class MCPToolCatalogSnapshot:
    definitions: tuple[MCPToolDefinition, ...]
    checked_at: str


def server_config_fingerprint(config: MCPServerConfig) -> str:
    config_data = config.model_dump(mode="json")
    # Tool Schema 不取决于凭据值；指纹只保留配置了哪些键，避免缓存派生敏感值。
    config_data["env"] = sorted(config.env)
    config_data["headers"] = sorted(config.headers)
    payload = json.dumps(
        config_data,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MCPToolCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self, name: str, config: MCPServerConfig) -> MCPToolCatalogSnapshot | None:
        path = self._path(name)
        try:
            if path.stat().st_size > _MAX_CATALOG_BYTES:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != _SCHEMA_VERSION or payload.get("server") != name:
            return None
        if payload.get("config_fingerprint") != server_config_fingerprint(config):
            return None
        raw_tools = payload.get("tools")
        if not isinstance(raw_tools, list):
            return None
        definitions: list[MCPToolDefinition] = []
        try:
            for raw in raw_tools:
                definition = self._parse_definition(raw)
                Draft202012Validator.check_schema(definition.input_schema)
                if definition.output_schema is not None:
                    Draft202012Validator.check_schema(definition.output_schema)
                definitions.append(definition)
        except (TypeError, ValueError, SchemaError):
            return None
        return MCPToolCatalogSnapshot(tuple(definitions), str(payload.get("checked_at", "")))

    def save(
        self,
        name: str,
        config: MCPServerConfig,
        definitions: tuple[MCPToolDefinition, ...],
    ) -> MCPToolCatalogSnapshot:
        checked_at = datetime.now(UTC).isoformat()
        payload = {
            "version": _SCHEMA_VERSION,
            "server": name,
            "config_fingerprint": server_config_fingerprint(config),
            "checked_at": checked_at,
            "tools": [self._serialize_definition(item) for item in definitions],
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > _MAX_CATALOG_BYTES:
            raise ValueError(f"MCP server {name} 工具目录超过缓存上限")
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        try:
            temp.write_bytes(encoded)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return MCPToolCatalogSnapshot(definitions, checked_at)

    def remove(self, name: str) -> bool:
        path = self._path(name)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def _path(self, name: str) -> Path:
        readable = _SAFE_NAME.sub("-", name).strip("-.")[:40] or "server"
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        return self.root / f"{readable}-{digest}.json"

    @staticmethod
    def _serialize_definition(item: MCPToolDefinition) -> dict[str, Any]:
        return {
            "name": item.raw_name,
            "description": item.description,
            "input_schema": item.input_schema,
            "output_schema": item.output_schema,
            "annotations": item.annotations or {},
        }

    @staticmethod
    def _parse_definition(raw: object) -> MCPToolDefinition:
        if not isinstance(raw, dict):
            raise TypeError("工具目录条目必须是对象")
        name = raw.get("name")
        description = raw.get("description", "")
        input_schema = raw.get("input_schema")
        output_schema = raw.get("output_schema")
        annotations = raw.get("annotations", {})
        if not isinstance(name, str) or not name:
            raise ValueError("工具目录缺少名称")
        if not isinstance(description, str) or not isinstance(input_schema, dict):
            raise TypeError("工具目录字段类型错误")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise TypeError("output_schema 类型错误")
        if not isinstance(annotations, dict):
            raise TypeError("annotations 类型错误")
        allowed = {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        safe_annotations = {key: annotations[key] for key in allowed if key in annotations}
        return MCPToolDefinition(
            raw_name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            annotations=safe_annotations,
        )
