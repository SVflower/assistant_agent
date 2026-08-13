"""Skill 与 MCP 的 slash 扩展控制面。"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Protocol, cast

from assistant_agent.application.ports import MCPRuntimePort
from assistant_agent.config.schema import MCPServerConfig
from assistant_agent.config.writer import ConfigScope, ConfigWriteError, SkillsConfigStore
from assistant_agent.integrations.mcp import MCPConfigureError, MCPService
from assistant_agent.integrations.skills import SkillInstallError, SkillManager
from assistant_agent.integrations.skills.manager import SkillScope
from assistant_agent.ui.console import Console

_PLAYWRIGHT_MCP_VERSION = "0.0.78"


class ExtensionCommandContext(Protocol):
    console: Console
    skills: list[tuple[str, str]]
    mcp_servers: list[tuple[str, list[str]]]
    mcp_runtime: MCPRuntimePort | None
    skill_manager: SkillManager | None
    skills_config_store: SkillsConfigStore | None
    mcp_service: MCPService | None
    runtime_generation: int


def cmd_skills(args: str, ctx: ExtensionCommandContext) -> None:
    """列出、安装、卸载或诊断 Skill。"""
    tokens = shlex.split(args)
    action = tokens[0].lower() if tokens else "list"
    if action == "list":
        _list_skills(ctx)
        return
    manager = ctx.skill_manager
    if manager is None:
        ctx.console.error("当前 Runtime 未启用 Skill 管理服务。")
        return
    if action == "doctor":
        roots = [manager.root("project"), manager.root("user")]
        lines = ["Skill 目录诊断：", *(f"  {root}" for root in roots)]
        trusted_names = set(ctx.skills_config_store.trusted()) if ctx.skills_config_store else set()
        project_names, invalid = _project_skill_diagnostics(manager)
        untrusted = sorted(project_names - trusted_names)
        if untrusted:
            lines.append("  未加载原因：以下 project Skill 尚未显式信任：")
            lines.extend(f"    {name}（执行 /skills trust {name} project）" for name in untrusted)
        else:
            lines.append("  没有未信任的 project Skill。")
        if invalid:
            lines.append("  无效的 project Skill：")
            lines.extend(f"    {name}（{reason}）" for name, reason in invalid)
        ctx.console.command_info("\n".join(lines))
        return
    if action not in {"install", "remove", "trust", "untrust"} or len(tokens) < 2:
        ctx.console.error(
            "用法：/skills list|doctor|install <目录> [user|project]|remove <名> [scope]|"
            "trust|untrust <名> [project]"
        )
        return
    options = tokens[2:]
    default_scope = "project" if action in {"trust", "untrust"} else "user"
    scope_value = next((item for item in options if item in {"user", "project"}), default_scope)
    scope = _scope(scope_value, ctx)
    if scope is None:
        return
    try:
        if action in {"trust", "untrust"}:
            if scope != "project":
                raise SkillInstallError("Skill trust/untrust 仅支持 project scope")
            config_store = ctx.skills_config_store
            if config_store is None:
                raise ConfigWriteError("当前 Runtime 未启用 project Skill 信任配置")
            name = tokens[1]
            manager.project_skill(name)
            changed = config_store.set_trusted(name, action == "trust")
            state = "true" if action == "trust" else "false"
            verb = "已更新" if changed else "无需变更"
            ctx.console.command_info(
                f"{verb} Skill {name}（scope=project，trusted={state}）。\n"
                "执行 /reload skills 更新当前 CLI Runtime。"
            )
            return
        if action == "install":
            if not _confirmed(
                ctx,
                f"确认检查并安装 Skill {tokens[1]}（{scope}）？\n"
                "Skill 可包含指令、脚本和模板；安装表示信任该来源。",
            ):
                ctx.console.command_info("已取消。")
                return
            if scope == "project" and ctx.skills_config_store is None:
                raise ConfigWriteError("当前 Runtime 未启用 project Skill 信任配置")
            result = manager.install(Path(tokens[1]), scope)
            try:
                if scope == "project":
                    assert ctx.skills_config_store is not None
                    ctx.skills_config_store.set_trusted(result.name, True)
            except Exception:
                if result.changed:
                    manager.uninstall(result.name, scope)
                raise
            verb = "已安装" if result.changed else "已是相同版本"
            lines = [
                f"{verb} Skill {result.name}\n  scope={scope}\n  path={result.path}\n  trusted=true"
            ]
            lines.append(_refresh_extensions(ctx, "skills"))
            ctx.console.command_info("\n".join(lines))
            return
        if not _confirmed(ctx, f"确认卸载受管 Skill {tokens[1]}（{scope}）？"):
            ctx.console.command_info("已取消。")
            return
        loaded = any(name == tokens[1] for name, _description in ctx.skills)
        manager.uninstall(tokens[1], scope)
        if scope == "project" and ctx.skills_config_store is not None:
            ctx.skills_config_store.set_trusted(tokens[1], False)
        ctx.console.command_info(
            f"已卸载 Skill {tokens[1]}（scope={scope}，loaded={str(loaded).lower()}）。\n"
            + _refresh_extensions(ctx, "skills")
        )
    except (ConfigWriteError, SkillInstallError, OSError) as exc:
        ctx.console.error(str(exc))


def _list_skills(ctx: ExtensionCommandContext) -> None:
    installed: dict[str, list[str]] = {}
    if ctx.skill_manager is not None:
        for scope in ("project", "user"):
            root = ctx.skill_manager.root(cast(SkillScope, scope))
            try:
                skill_files = sorted(root.glob("*/SKILL.md")) if root.is_dir() else []
            except OSError:
                skill_files = []
            for skill_file in skill_files:
                installed.setdefault(skill_file.parent.name, []).append(scope)
    loaded = {name: description for name, description in ctx.skills}
    trusted_project = set(ctx.skills_config_store.trusted()) if ctx.skills_config_store else set()
    if not installed and not loaded:
        ctx.console.command_info(
            "未发现技能。项目 Skill 放到 ./skills/<名>/；个人 Skill 放到 "
            "~/.assistant_agent/skills/<名>/。"
        )
        return
    lines = [f"Skills · Runtime generation {ctx.runtime_generation}："]
    for name in sorted(set(installed) | set(loaded)):
        scopes = ",".join(installed.get(name, [])) or "configured"
        trusted = "project" not in installed.get(name, []) or name in trusted_project
        state = "loaded" if name in loaded else "not-loaded"
        description = loaded.get(name, "")
        lines.append(
            f"  {name:<16} {state} · scope={scopes} · trusted={str(trusted).lower()} "
            f"{description}".rstrip()
        )
    ctx.console.command_info("\n".join(lines))


def _project_skill_diagnostics(manager: SkillManager) -> tuple[set[str], list[tuple[str, str]]]:
    root = manager.root("project")
    try:
        names = (
            sorted(path.parent.name for path in root.glob("*/SKILL.md")) if root.is_dir() else []
        )
    except OSError:
        return set(), []
    valid: set[str] = set()
    invalid: list[tuple[str, str]] = []
    for name in names:
        try:
            manager.project_skill(name)
            valid.add(name)
        except SkillInstallError as exc:
            invalid.append((name, str(exc)))
    return valid, invalid


def cmd_mcp(args: str, ctx: ExtensionCommandContext) -> None:
    """列出、探测和管理 MCP server。"""
    tokens = shlex.split(args)
    action = tokens[0].lower() if tokens else "list"
    if action == "list":
        _list_mcp(ctx)
        return
    service = ctx.mcp_service
    if service is None:
        ctx.console.error("当前 Runtime 未启用 MCP 配置服务。")
        return
    if action == "add":
        _mcp_add(tokens[1:], ctx, service)
        return
    if action in {"test", "doctor"}:
        _mcp_test(tokens[1:], ctx, service)
        return
    if action not in {"enable", "disable", "trust", "untrust", "remove"}:
        ctx.console.error("用法：/mcp list|add|test|doctor|enable|disable|trust|untrust|remove")
        return
    if len(tokens) < 2:
        ctx.console.error(f"用法：/mcp {action} <server> [user|project]")
        return
    options = tokens[2:]
    scope_value = next((item for item in options if item in {"user", "project"}), "user")
    scope = _scope(scope_value, ctx)
    if scope is None:
        return
    name = tokens[1]
    if not _confirmed(ctx, f"确认{action} MCP server {name}（{scope}）？"):
        ctx.console.command_info("已取消。")
        return
    try:
        runtime_status = _runtime_mcp_status(ctx, name)
        if action == "remove":
            if not service.remove(name, scope):
                raise MCPConfigureError(f"{scope} scope 中不存在 MCP server：{name}")
            ctx.console.command_info(
                f"已移除 MCP server {name}（scope={scope}，connected={runtime_status}）。"
                "历史 artifact 保留；使用 /reload mcp 从当前 Runtime 移除。"
            )
            if "--purge-artifacts" in options:
                if _confirmed(ctx, f"再次确认永久清理 {name} 的当前工作区历史 artifact？"):
                    purged = service.purge_artifacts(name)
                    message = "已清理历史 artifact。" if purged else "没有可清理的历史 artifact。"
                    ctx.console.command_info(message)
                else:
                    ctx.console.command_info("已保留历史 artifact。")
        elif action in {"enable", "disable"}:
            service.set_enabled(name, action == "enable", scope)
            ctx.console.command_info(
                f"已{action} MCP server {name}（scope={scope}）。"
                "使用 /reload mcp 刷新当前 CLI Runtime。"
            )
        else:
            service.set_trusted(name, action == "trust", scope)
            ctx.console.command_info(
                f"已{action} MCP server {name}（scope={scope}）。"
                "使用 /reload mcp 刷新当前 CLI Runtime。"
            )
    except (MCPConfigureError, ConfigWriteError, OSError) as exc:
        ctx.console.error(str(exc))


def _list_mcp(ctx: ExtensionCommandContext) -> None:
    configured = ctx.mcp_service.list() if ctx.mcp_service is not None else {}
    running = dict(ctx.mcp_servers)
    runtime = getattr(ctx, "mcp_runtime", None)
    statuses = (
        {item.name: item for item in runtime.server_capabilities()} if runtime is not None else {}
    )
    if not configured and not running and not statuses:
        ctx.console.command_info(
            "未接入 MCP server。可用 /mcp add playwright 安装 Playwright MCP。"
        )
        return
    lines: list[str] = [f"MCP · Runtime generation {ctx.runtime_generation}："]
    total_tools = 0
    names = sorted(set(configured) | set(running) | set(statuses))
    for name in names:
        item = configured.get(name)
        source = item[0] if item else "runtime"
        enabled = item[1].enabled if item else True
        trusted = item[1].auto_approve if item else False
        status = statuses.get(name)
        tools = list(status.tool_names) if status is not None else running.get(name, [])
        total_tools += len(tools)
        state = status.status if status is not None else ("enabled" if enabled else "disabled")
        trust = " · trusted" if trusted else ""
        detail = f"：{', '.join(tools)}" if tools else ""
        lines.append(f"  {name}（{len(tools)} 个工具） · {source} · {state}{trust}{detail}")
    lines.insert(1, f"MCP server：{len(names)} 个；暴露工具：{total_tools} 个")
    ctx.console.command_info("\n".join(lines))


def _runtime_mcp_status(ctx: ExtensionCommandContext, name: str) -> str:
    runtime = getattr(ctx, "mcp_runtime", None)
    if runtime is None:
        return "false"
    status = next(
        (item.status for item in runtime.server_capabilities() if item.name == name), None
    )
    return "true" if status == "connected" else "false"


def _mcp_add(tokens: list[str], ctx: ExtensionCommandContext, service: MCPService) -> None:
    if not tokens:
        ctx.console.error(
            "用法：/mcp add playwright [scope]，或 /mcp add <name> <command|URL> [args...]"
        )
        return
    name = tokens[0]
    rest = tokens[1:]
    scope: ConfigScope = "user"
    if rest and rest[-1] in {"user", "project"}:
        scope = cast(ConfigScope, rest.pop())
    if name == "playwright" and not rest:
        server = MCPServerConfig(
            command="npx",
            args=["-y", f"@playwright/mcp@{_PLAYWRIGHT_MCP_VERSION}", "--headless"],
        )
    elif rest and rest[0].startswith(("http://", "https://")):
        server = MCPServerConfig(type="http", url=rest[0])
    elif rest:
        server = MCPServerConfig(command=rest[0], args=rest[1:])
    else:
        ctx.console.error("缺少 MCP 启动命令或 URL。")
        return
    preview = server.url if server.type == "http" else " ".join([server.command, *server.args])
    if not _confirmed(
        ctx,
        f"将探测并写入 {scope} MCP 配置：\n{preview}\n"
        "第三方 server 可执行代码或访问网络，未固定版本会产生供应链漂移。确认继续？",
    ):
        ctx.console.command_info("已取消。")
        return
    try:
        result = service.add(name, server, scope)
        ctx.console.command_info(
            f"已验证并添加 {name}\n  scope={scope}\n  config={service.store.path(scope)}\n"
            f"  discovered_tools={len(result.tools)}\n  connected=false\n"
            "使用 /reload mcp 立即连接并刷新当前 CLI Runtime。"
        )
    except (MCPConfigureError, ConfigWriteError, OSError) as exc:
        ctx.console.error(str(exc))


def _mcp_test(tokens: list[str], ctx: ExtensionCommandContext, service: MCPService) -> None:
    if not tokens:
        ctx.console.error("用法：/mcp test <server> [user|project]")
        return
    scope = _scope(tokens[1] if len(tokens) > 1 else "user", ctx)
    if scope is None:
        return
    try:
        server = service.store.get(tokens[0], scope)
        if server is None:
            raise MCPConfigureError(f"{scope} scope 中不存在 MCP server：{tokens[0]}")
        if not _confirmed(ctx, f"确认启动并探测 MCP server {tokens[0]}？"):
            ctx.console.command_info("已取消。")
            return
        result = service.probe(tokens[0], server)
        ctx.console.command_info(
            f"MCP server {tokens[0]} 验证通过：{len(result.tools)} tools\n"
            + "\n".join(f"  {name}" for name in result.tools)
        )
    except (MCPConfigureError, ConfigWriteError, OSError) as exc:
        ctx.console.error(str(exc))


def _scope(value: str, ctx: ExtensionCommandContext) -> ConfigScope | None:
    if value not in {"user", "project"}:
        ctx.console.error(f"未知 scope：{value}。可选：user, project")
        return None
    return cast(ConfigScope, value)


def _confirmed(ctx: ExtensionCommandContext, message: str) -> bool:
    return ctx.console.confirm(message) in {"allow", "always"}


def _refresh_extensions(ctx: ExtensionCommandContext, target: str) -> str:
    reload_runtime = getattr(ctx, "reload_runtime", None)
    if reload_runtime is None:
        return "已写入磁盘；将在下次 Runtime 启动时生效。"
    try:
        return reload_runtime(target)
    except Exception as exc:  # noqa: BLE001 - 安装结果保留，旧 Runtime 仍可用
        return f"自动刷新失败，当前 Runtime 保持可用：{exc}"
