"""Slash 命令系统：可发现的会话控制层。

对齐 Claude Code 的 `/` 命令——本地拦截、不进 ReAct、不花 token。
把散落的 /model、exit 收编成注册表，并用 /help 让所有命令自我暴露（可发现性）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.application.models import Session
from assistant_agent.application.ports import MCPRuntimePort, SessionRepository
from assistant_agent.cli.extensions import cmd_mcp, cmd_skills
from assistant_agent.config.schema import AppConfig
from assistant_agent.integrations.mcp import MCPService
from assistant_agent.integrations.skills import SkillManager
from assistant_agent.observability import NullLogger
from assistant_agent.providers.litellm import LLMClient
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.permissions import (
    PermissionMode,
    permission_mode_label,
)
from assistant_agent.ui.console import Console, DisplayMode


@dataclass
class ChatContext:
    """slash 命令执行时可操作的会话状态。

    命令通过它读写会话（切模型、清历史、看用量、退出），不直接依赖 main 局部变量。
    """

    config: AppConfig
    loop: AgentLoop
    console: Console
    store: SessionRepository
    session: Session
    logger: NullLogger = field(default_factory=NullLogger)
    skills: list[tuple[str, str]] = field(default_factory=list)  # (name, description)
    # (server 名, 其工具原始名列表)；MCP 禁用/无 server 时为空。
    mcp_servers: list[tuple[str, list[str]]] = field(default_factory=list)
    mcp_runtime: MCPRuntimePort | None = None
    skill_manager: SkillManager | None = None
    mcp_service: MCPService | None = None
    tool_context: ToolContext | None = None
    should_exit: bool = False


@dataclass
class SlashCommand:
    name: str  # 不含斜杠，如 "model"
    description: str
    handler: Callable[[str, ChatContext], None]  # (参数串, 上下文)


class SlashRegistry:
    """slash 命令集合：注册、分发、渲染 /help。"""

    def __init__(self) -> None:
        self._cmds: dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand) -> None:
        self._cmds[cmd.name] = cmd

    def names(self) -> list[str]:
        return list(self._cmds)

    def descriptions(self) -> list[tuple[str, str]]:
        """返回 UI 可消费的命令名和说明，不暴露 handler。"""
        return [(f"/{cmd.name}", cmd.description) for cmd in self._cmds.values()]

    def dispatch(self, text: str, ctx: ChatContext) -> None:
        """处理一条以 / 开头的输入。未知命令给友好提示，绝不进 ReAct。"""
        body = text[1:] if text.startswith("/") else text
        parts = body.split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""

        if name in ("", "help"):
            self._render_help(ctx.console)
            return
        cmd = self._cmds.get(name)
        if cmd is None:
            ctx.console.error(f"未知命令：/{name}，输入 /help 查看可用命令")
            return
        cmd.handler(args, ctx)

    def _render_help(self, console: Console) -> None:
        lines = ["可用命令（输入 / 前缀，本地执行、不消耗 token）："]
        lines += [f"  /{c.name:<10} {c.description}" for c in self._cmds.values()]
        console.command_info("\n".join(lines))


# ---- 内置命令 handler ----


def _cmd_model(args: str, ctx: ChatContext) -> None:
    """切换模型（保留上下文）。无参弹菜单，带名直切。"""
    names = sorted(ctx.config.providers)
    target = args
    if not target:
        if not sys.stdin.isatty():
            ctx.console.command_info(f"非交互环境，请用 /model <名>。可选：{', '.join(names)}")
            return
        choices = [f"{n}（当前）" if n == ctx.config.active else n for n in names]
        picked = ctx.console.ask_question("切换到哪个 provider？", choices)
        target = picked.replace("（当前）", "").strip()
    if target not in ctx.config.providers:
        ctx.console.error(f"未知 provider：{target}。可选：{', '.join(names)}")
        return
    if target == ctx.config.active:
        ctx.console.command_info(f"已在使用 {target}，无需切换。")
        return
    previous_provider = ctx.config.active
    previous_model = ctx.config.active_provider.model
    ctx.config.active = target
    ctx.loop.set_client(LLMClient(ctx.config.active_provider))
    ctx.session.provider = target
    ctx.session.model = ctx.config.active_provider.model
    ctx.console.set_model_label(ctx.config.active_provider.model)
    ctx.logger.model_switch(
        from_provider=previous_provider,
        from_model=previous_model,
        to_provider=target,
        to_model=ctx.config.active_provider.model,
    )
    ctx.console.command_info(
        f"已切换到 {target}（{ctx.config.active_provider.model}），对话上下文保留。"
    )


def _cmd_sessions(args: str, ctx: ChatContext) -> None:
    """列出历史会话。"""
    metas = ctx.store.list()
    if not metas:
        ctx.console.command_info("暂无历史会话。")
        return
    ctx.console.print_sessions(metas)


def _cmd_clear(args: str, ctx: ChatContext) -> None:
    """开一个新会话（清空当前上下文）。旧会话文件仍在，可 /sessions 查看。"""
    ctx.session = ctx.store.new_session(
        provider=ctx.config.active, model=ctx.config.active_provider.model
    )
    ctx.loop.load_history([])
    ctx.loop.load_checkpoint(None)  # M8b：新会话清掉摘要 checkpoint
    ctx.logger.bind_session(ctx.session.id)
    ctx.store.save(ctx.session, [], must_exist=False)
    ctx.console.command_info(f"已开新会话 {ctx.session.id}，上下文已清空。")


def _cmd_context(args: str, ctx: ChatContext) -> None:
    """查看当前会话状态：消息数、模型、上下文预算分项占用。"""
    n = len(ctx.loop.export_history())
    r = ctx.loop.context_report()
    total = r["total"] or 1  # 防除零
    pct = round(r["used"] * 100 / total)
    compacted = "（早前对话已压缩为摘要）" if r.get("compacted") else ""
    ctx.console.command_info(
        f"当前会话：{n} 条消息 · 模型 {ctx.config.active_provider.model}{compacted}\n"
        f"上下文占用 {r['used']}/{r['total']} tokens（约 {pct}%）：\n"
        f"  system {r['system']} · tools {r['tools']} · "
        f"messages {r['messages']} · reserved(预留回复) {r['reserved']}"
    )


def _cmd_display(args: str, ctx: ChatContext) -> None:
    """查看或切换当前会话的展示密度。"""
    modes = ("normal", "verbose", "quiet")
    target = args.strip().lower()
    if not target:
        ctx.console.command_info(
            f"当前展示模式：{ctx.console.display_mode}。可选：{', '.join(modes)}"
        )
        return
    if target not in modes:
        ctx.console.error(f"未知展示模式：{target}。可选：{', '.join(modes)}")
        return
    mode = cast(DisplayMode, target)
    ctx.console.set_display_mode(mode, force=True)
    ctx.config.ui.display_mode = mode
    ctx.console.command_info(f"展示模式已切换为 {target}。")


def _cmd_permissions(args: str, ctx: ChatContext) -> None:
    """查看或切换当前 CLI Runtime 的权限模式。"""
    aliases: dict[str, PermissionMode] = {
        "readonly": "readonly",
        "workspace": "workspace",
        "ask": "strict",
        "full": "unrestricted",
    }
    target = args.strip().lower()
    if ctx.tool_context is None:
        ctx.console.error("当前入口不支持动态权限切换。")
        return
    if not target:
        current = ctx.tool_context.permission_policy.mode
        options = "、".join(
            f"{name}（{permission_mode_label(mode)}）" for name, mode in aliases.items()
        )
        ctx.console.command_info(
            f"当前权限模式：{permission_mode_label(current)}（{current}）。\n"
            f"可选：{options}\n用法：/permissions <readonly|workspace|ask|full>"
        )
        return
    mode = aliases.get(target)
    if mode is None:
        ctx.console.error(
            f"未知权限模式：{target}。用法：/permissions <readonly|workspace|ask|full>"
        )
        return
    ctx.tool_context.permission_policy.mode = mode
    message = (
        f"权限模式已切换为 {permission_mode_label(mode)}（{mode}）。"
        "已有本会话授权保持有效；显式 deny 规则仍优先。"
    )
    if mode == "unrestricted":
        message += (
            "\n危险：完全访问会默认允许高风险工具操作。"
            "仅当前 CLI Runtime 生效，不影响 Web/API，退出后恢复配置默认值。"
        )
    else:
        message += "仅当前 CLI Runtime 生效，退出后恢复配置默认值。"
    ctx.console.command_info(message)


def _cmd_exit(args: str, ctx: ChatContext) -> None:
    """退出交互模式。"""
    ctx.should_exit = True


def build_default_slash_registry() -> SlashRegistry:
    """构建内置 slash 命令表。/help 由 dispatch 内建处理。"""
    reg = SlashRegistry()
    reg.register(SlashCommand("help", "列出所有命令", lambda _a, _c: None))  # 由 dispatch 处理
    reg.register(SlashCommand("model", "切换模型（保留上下文）", _cmd_model))
    reg.register(SlashCommand("sessions", "列出历史会话", _cmd_sessions))
    reg.register(SlashCommand("clear", "开新会话（清空上下文）", _cmd_clear))
    reg.register(SlashCommand("context", "查看会话状态与用量", _cmd_context))
    reg.register(SlashCommand("skills", "管理 Skill（list/install/remove/doctor）", cmd_skills))
    reg.register(SlashCommand("mcp", "管理 MCP server（list/add/test/remove 等）", cmd_mcp))
    reg.register(SlashCommand("display", "查看或切换展示模式", _cmd_display))
    reg.register(SlashCommand("permissions", "查看或切换当前 Runtime 权限模式", _cmd_permissions))
    reg.register(SlashCommand("exit", "退出（也可输入 exit/quit）", _cmd_exit))
    return reg
