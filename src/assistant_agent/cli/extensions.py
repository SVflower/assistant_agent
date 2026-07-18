"""Skill 与 MCP 的 slash 扩展控制面。"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Protocol, cast

from assistant_agent.config.schema import MCPServerConfig
from assistant_agent.config.writer import ConfigScope, ConfigWriteError
from assistant_agent.integrations.mcp import MCPConfigureError, MCPService
from assistant_agent.integrations.skills import SkillInstallError, SkillManager
from assistant_agent.ui.console import Console


class ExtensionCommandContext(Protocol):
    console: Console
    skills: list[tuple[str, str]]
    mcp_servers: list[tuple[str, list[str]]]
    skill_manager: SkillManager | None
    mcp_service: MCPService | None


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
        legacy = Path.cwd() / ".assistant_agent" / "skills"
        lines = ["Skill 目录诊断：", *(f"  {root}" for root in roots)]
        if legacy.exists():
            lines.append(f"  发现旧只读目录：{legacy}（建议迁移，不会自动删除）")
        ctx.console.command_info("\n".join(lines))
        return
    if action not in {"install", "remove"} or len(tokens) < 2:
        ctx.console.error(
            "用法：/skills list|doctor|install <目录> [user|project]|remove <名> [scope]"
        )
        return
    options = tokens[2:]
    scope_value = next((item for item in options if item in {"user", "project"}), "user")
    scope = _scope(scope_value, ctx)
    if scope is None:
        return
    try:
        if action == "install":
            result = manager.install(Path(tokens[1]), scope)
            verb = "已安装" if result.changed else "已是相同版本"
            ctx.console.command_info(
                f"{verb} Skill {result.name}（{scope}）：{result.path}\n下次启动生效。"
            )
            return
        if not _confirmed(ctx, f"确认卸载受管 Skill {tokens[1]}（{scope}）？"):
            ctx.console.command_info("已取消。")
            return
        manager.uninstall(tokens[1], scope)
        ctx.console.command_info(f"已卸载 Skill {tokens[1]}（{scope}）。下次启动生效。")
    except (SkillInstallError, OSError) as exc:
        ctx.console.error(str(exc))


def _list_skills(ctx: ExtensionCommandContext) -> None:
    if not ctx.skills:
        ctx.console.command_info(
            "未发现技能。项目 Skill 放到 ./.agents/skills/<名>/；个人 Skill 放到 "
            "~/.assistant_agent/skills/<名>/。"
        )
        return
    lines = ["已发现技能（模型会按需 load_skill 加载）："]
    lines += [f"  {name:<16} {description}" for name, description in ctx.skills]
    ctx.console.command_info("\n".join(lines))


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
    if not _confirmed(ctx, f"确认{action} MCP server {name}（{scope}）？下次启动生效。"):
        ctx.console.command_info("已取消。")
        return
    try:
        if action == "remove":
            if not service.remove(name, scope):
                raise MCPConfigureError(f"{scope} scope 中不存在 MCP server：{name}")
            ctx.console.command_info(
                f"已移除 MCP server {name}（{scope}）。历史 artifact 保留；下次启动生效。"
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
            ctx.console.command_info(f"已{action} MCP server {name}。下次启动生效。")
        else:
            service.set_trusted(name, action == "trust", scope)
            ctx.console.command_info(f"已{action} MCP server {name}。下次启动生效。")
    except (MCPConfigureError, ConfigWriteError, OSError) as exc:
        ctx.console.error(str(exc))


def _list_mcp(ctx: ExtensionCommandContext) -> None:
    configured = ctx.mcp_service.list() if ctx.mcp_service is not None else {}
    running = dict(ctx.mcp_servers)
    if not configured and not running:
        ctx.console.command_info(
            "未接入 MCP server。可用 /mcp add playwright 安装 Playwright MCP。"
        )
        return
    lines = ["MCP server："]
    names = sorted(set(configured) | set(running))
    for name in names:
        item = configured.get(name)
        source = item[0] if item else "runtime"
        enabled = item[1].enabled if item else True
        trusted = item[1].auto_approve if item else False
        tools = running.get(name, [])
        state = "running" if name in running else ("enabled" if enabled else "disabled")
        trust = " · trusted" if trusted else ""
        detail = f"：{', '.join(tools)}" if tools else ""
        lines.append(f"  {name}（{len(tools)} 个工具） · {source} · {state}{trust}{detail}")
    ctx.console.command_info("\n".join(lines))


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
        server = MCPServerConfig(command="npx", args=["-y", "@playwright/mcp@latest", "--headless"])
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
            f"已验证并添加 {name}（{scope}），发现 {len(result.tools)} 个工具。下次启动生效。"
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
