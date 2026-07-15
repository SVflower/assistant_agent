"""配置加载：读取 YAML、解析环境变量占位、校验。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from assistant_agent.config.paths import assistant_home
from assistant_agent.config.schema import AppConfig

# 匹配 ${VAR} 或 ${VAR:-default} 形式的环境变量占位
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_NAME = "config.yaml"


class ConfigError(Exception):
    """配置加载或校验失败。"""


def _expand_env(value: str) -> str:
    """把字符串里的 ${VAR} / ${VAR:-default} 替换成环境变量值。"""

    def repl(match: re.Match[str]) -> str:
        var, default = match.group(1), match.group(2)
        return os.environ.get(var, default if default is not None else "")

    return _ENV_PATTERN.sub(repl, value)


def _expand_env_recursive(obj: object) -> object:
    """递归展开 dict / list / str 中的环境变量占位。"""
    if isinstance(obj, dict):
        return {k: _expand_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_recursive(v) for v in obj]
    if isinstance(obj, str):
        return _expand_env(obj)
    return obj


def _migrate_legacy_fields(raw: dict[object, object]) -> dict[object, object]:
    """迁移仍在本地配置中的旧字段，不把兼容字段留进公共 schema。"""
    migrated = dict(raw)
    agent_raw = migrated.get("agent")
    tools_raw = migrated.get("tools")
    agent = dict(agent_raw) if isinstance(agent_raw, dict) else {}
    tools = dict(tools_raw) if isinstance(tools_raw, dict) else {}

    legacy_output_limit = agent.pop("max_tool_output_chars", None)
    if legacy_output_limit is not None and "max_output_chars" not in tools:
        tools["max_output_chars"] = legacy_output_limit

    if isinstance(agent_raw, dict) or agent:
        migrated["agent"] = agent
    if isinstance(tools_raw, dict) or tools:
        migrated["tools"] = tools
    return migrated


def _merge_user_mcp(raw: dict[object, object]) -> dict[object, object]:
    """合并用户级 MCP 定义；项目同名 server 覆盖 user。"""
    path = assistant_home() / "mcp" / "servers.yaml"
    if not path.is_file():
        return raw
    try:
        user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"用户 MCP 配置读取失败（{path}）：{exc}") from exc
    if not isinstance(user, dict):
        raise ConfigError(f"用户 MCP 配置根节点必须是映射（{path}）。")
    user_servers = user.get("servers", {}) if isinstance(user, dict) else {}
    if not isinstance(user_servers, dict):
        raise ConfigError(f"用户 MCP servers 必须是映射（{path}）。")
    merged = dict(raw)
    mcp_raw = merged.get("mcp")
    mcp = dict(mcp_raw) if isinstance(mcp_raw, dict) else {}
    project_servers = mcp.get("servers", {})
    project_servers = dict(project_servers) if isinstance(project_servers, dict) else {}
    mcp["servers"] = {**user_servers, **project_servers}
    merged["mcp"] = mcp
    return merged


def find_config_file(start: Path | None = None) -> Path | None:
    """从指定目录（默认 cwd）向上查找 config.yaml。"""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载并校验配置。

    Args:
        path: 配置文件路径；为 None 时自动向上查找 config.yaml。

    Raises:
        ConfigError: 文件不存在、YAML 解析失败或校验失败。
    """
    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise ConfigError(f"配置文件不存在：{config_path}")
    else:
        found = find_config_file()
        if found is None:
            raise ConfigError(
                f"未找到 {DEFAULT_CONFIG_NAME}。请复制 config.example.yaml 为 config.yaml 并填写。"
            )
        config_path = found

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败（{config_path}）：{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"配置根节点必须是映射（{config_path}）。")

    expanded = _expand_env_recursive(_merge_user_mcp(_migrate_legacy_fields(raw)))

    try:
        return AppConfig.model_validate(expanded)
    except ValidationError as exc:
        raise ConfigError(f"配置校验失败（{config_path}）：\n{exc}") from exc
