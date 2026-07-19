"""MCP 工具发现后的过滤、校验和适配。"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from assistant_agent.config.schema import MCPConfig, MCPServerConfig
from assistant_agent.integrations.mcp.models import MCPToolDefinition
from assistant_agent.integrations.mcp.tool import MCPTool
from assistant_agent.tools.validation import ToolSchemaError, build_validator

_NAME_SANITIZE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize(part: str) -> str:
    return _NAME_SANITIZE.sub("_", part)


def build_discovered_tools(
    *,
    config: MCPConfig,
    name: str,
    server_config: MCPServerConfig,
    server: Any,
    listed: Any,
    used_names: set[str],
    budget: int,
    warnings: list[str],
    caller: Callable[..., Any],
) -> list[MCPTool]:
    definitions = definitions_from_listed(listed)
    return build_tools_from_definitions(
        config=config,
        name=name,
        server_config=server_config,
        definitions=definitions,
        tool_names=server.tool_names,
        used_names=used_names,
        budget=budget,
        warnings=warnings,
        caller=caller,
    )


def definitions_from_listed(listed: Any) -> tuple[MCPToolDefinition, ...]:
    return tuple(
        MCPToolDefinition(
            raw_name=str(raw.name),
            description=str(raw.description or ""),
            input_schema=raw.inputSchema or {"type": "object", "properties": {}},
            output_schema=getattr(raw, "outputSchema", None),
            annotations=_tool_annotations(getattr(raw, "annotations", None)),
        )
        for raw in (getattr(listed, "tools", None) or [])
    )


def build_tools_from_definitions(
    *,
    config: MCPConfig,
    name: str,
    server_config: MCPServerConfig,
    definitions: tuple[MCPToolDefinition, ...],
    tool_names: list[str],
    used_names: set[str],
    budget: int,
    warnings: list[str],
    caller: Callable[..., Any],
) -> list[MCPTool]:
    out: list[MCPTool] = []
    server_slug = _sanitize(name)
    include = set(server_config.include_tools)
    exclude = set(server_config.exclude_tools)
    for raw in definitions:
        raw_name = raw.raw_name
        if include and raw_name not in include:
            continue
        if raw_name in exclude:
            continue
        input_schema = raw.input_schema
        output_schema = raw.output_schema
        try:
            build_validator(f"mcp__{name}__{raw_name}", input_schema)
            if output_schema is not None:
                build_validator(f"mcp__{name}__{raw_name} output", output_schema)
        except ToolSchemaError as exc:
            warnings.append(f"MCP 工具 {name}/{raw_name} schema 无效，已跳过：{exc}")
            continue
        if len(out) >= server_config.max_tools:
            warnings.append(f"server {name} 达工具上限 {server_config.max_tools}，其余丢弃")
            break
        if budget + len(out) >= config.max_total_tools:
            warnings.append(f"达全局工具上限 {config.max_total_tools}，{name} 部分工具丢弃")
            break
        registered = f"mcp__{server_slug}__{_sanitize(raw_name)}"
        if registered in used_names:
            suffix = 2
            while f"{registered}_{suffix}" in used_names:
                suffix += 1
            registered = f"{registered}_{suffix}"
        used_names.add(registered)
        tool_names.append(raw_name)
        annotations = raw.annotations or {}
        policy = server_config.tool_policies.get(raw_name)
        replay = policy.replay if policy is not None else "default"
        destructive = annotations.get("destructiveHint") is True
        trusted_readonly = replay == "safe_readonly" or (
            replay == "default"
            and server_config.trust_tool_annotations
            and annotations.get("readOnlyHint") is True
            and not destructive
        )
        if replay == "requires_decision" or destructive:
            trusted_readonly = False
        outcome_unknown = not trusted_readonly
        if policy is not None and policy.outcome_on_transport_error == "unknown":
            outcome_unknown = True
        timeout = float(
            policy.timeout if policy is not None and policy.timeout else server_config.timeout
        )
        out.append(
            MCPTool(
                server=name,
                registered_name=registered,
                raw_tool=raw_name,
                description=raw.description,
                input_schema=input_schema,
                caller=caller,
                timeout=timeout,
                auto_approve=server_config.auto_approve and not destructive,
                output_schema=output_schema,
                annotations=annotations,
                trusted_readonly=trusted_readonly,
                outcome_unknown_on_transport_error=outcome_unknown,
            )
        )
    return out


def _tool_annotations(value: Any) -> dict[str, Any]:
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
