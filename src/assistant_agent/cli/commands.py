"""Slash 命令系统：可发现的会话控制层。

对齐 Claude Code 的 `/` 命令——本地拦截、不进 ReAct、不花 token。
把散落的 /model、exit 收编成注册表，并用 /help 让所有命令自我暴露（可发现性）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMClient
from assistant_agent.session.store import Session, SessionStore
from assistant_agent.ui.console import Console


@dataclass
class ChatContext:
    """slash 命令执行时可操作的会话状态。

    命令通过它读写会话（切模型、清历史、看用量、退出），不直接依赖 main 局部变量。
    """

    config: AppConfig
    loop: AgentLoop
    console: Console
    store: SessionStore
    session: Session
    skills: list[tuple[str, str]] = field(default_factory=list)  # (name, description)
    # (server 名, 其工具原始名列表)；MCP 禁用/无 server 时为空。
    mcp_servers: list[tuple[str, list[str]]] = field(default_factory=list)
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
        console.info("\n".join(lines))


# ---- 内置命令 handler ----


def _cmd_model(args: str, ctx: ChatContext) -> None:
    """切换模型（保留上下文）。无参弹菜单，带名直切。"""
    names = sorted(ctx.config.providers)
    target = args
    if not target:
        if not sys.stdin.isatty():
            ctx.console.info(f"非交互环境，请用 /model <名>。可选：{', '.join(names)}")
            return
        choices = [f"{n}（当前）" if n == ctx.config.active else n for n in names]
        picked = ctx.console.ask_question("切换到哪个 provider？", choices)
        target = picked.replace("（当前）", "").strip()
    if target not in ctx.config.providers:
        ctx.console.error(f"未知 provider：{target}。可选：{', '.join(names)}")
        return
    if target == ctx.config.active:
        ctx.console.info(f"已在使用 {target}，无需切换。")
        return
    ctx.config.active = target
    ctx.loop.set_client(LLMClient(ctx.config.active_provider))
    ctx.console.info(f"已切换到 {target}（{ctx.config.active_provider.model}），对话上下文保留。")


def _cmd_sessions(args: str, ctx: ChatContext) -> None:
    """列出历史会话。"""
    metas = ctx.store.list()
    if not metas:
        ctx.console.info("暂无历史会话。")
        return
    ctx.console.print_sessions(metas)


def _cmd_clear(args: str, ctx: ChatContext) -> None:
    """开一个新会话（清空当前上下文）。旧会话文件仍在，可 /sessions 查看。"""
    ctx.session = ctx.store.new_session(
        provider=ctx.config.active, model=ctx.config.active_provider.model
    )
    ctx.loop.load_history([])
    ctx.console.info(f"已开新会话 {ctx.session.id}，上下文已清空。")


def _cmd_context(args: str, ctx: ChatContext) -> None:
    """查看当前会话状态：消息数、模型、上下文预算。"""
    n = len(ctx.loop.export_history())
    ctx.console.info(
        f"当前会话：{n} 条消息 · 模型 {ctx.config.active_provider.model} · "
        f"上下文预算 {ctx.config.agent.max_context_tokens} tokens"
    )


def _cmd_skills(args: str, ctx: ChatContext) -> None:
    """列出已发现的技能（name + description）。"""
    if not ctx.skills:
        ctx.console.info(
            "未发现技能。把 SKILL.md 放到 ./.assistant_agent/skills/<名>/ 或 "
            "~/.assistant_agent/skills/<名>/ 下即可。"
        )
        return
    lines = ["已发现技能（模型会按需 load_skill 加载）："]
    lines += [f"  {name:<16} {description}" for name, description in ctx.skills]
    ctx.console.info("\n".join(lines))


def _cmd_mcp(args: str, ctx: ChatContext) -> None:
    """列出已接入的 MCP server 及其工具。"""
    if not ctx.mcp_servers:
        ctx.console.info("未接入 MCP server。在配置 mcp.servers 下添加并 enabled=true 即可。")
        return
    lines = ["已接入的 MCP server（工具以 mcp__<server>__<tool> 注册）："]
    for server, tools in ctx.mcp_servers:
        lines.append(f"  {server}（{len(tools)} 个工具）: {', '.join(tools) or '(无)'}")
    ctx.console.info("\n".join(lines))


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
    reg.register(SlashCommand("skills", "列出已发现的技能", _cmd_skills))
    reg.register(SlashCommand("mcp", "列出已接入的 MCP server 与工具", _cmd_mcp))
    reg.register(SlashCommand("exit", "退出（也可输入 exit/quit）", _cmd_exit))
    return reg
