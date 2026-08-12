"""终端输入输出，基于 Rich。

负责把 AgentLoop 的 StepEvent 渲染给用户，并提供危险操作的确认交互。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from rich.console import Console as RichConsole
from rich.text import Text

from assistant_agent.contracts.capabilities import RuntimeStartupEvent
from assistant_agent.contracts.events import StepEvent
from assistant_agent.tools.context import ConfirmChoice
from assistant_agent.ui.activity import suspend_active
from assistant_agent.ui.formatting import (
    build_banner,
    build_providers_table,
    build_sessions_table,
    read_input,
)

DisplayMode = Literal["normal", "verbose", "quiet"]


class Console:
    """对 Rich 的薄封装，集中所有终端输出。"""

    def __init__(
        self, show_reasoning: bool = False, display_mode: DisplayMode | None = None
    ) -> None:
        # Windows 终端默认 GBK，遇到中文/emoji 会抛 UnicodeEncodeError。
        # 把底层 stdout/stderr 重配为 UTF-8（Python 3.7+ 支持）。
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8")
                except (ValueError, OSError):
                    pass
        self._console = RichConsole()
        self._show_reasoning = show_reasoning
        self._display_mode: DisplayMode = display_mode or "normal"
        self._display_override = display_mode is not None
        # 上下文窗口预算，用于结尾显示"上下文占用 %"（由 main 按 config 注入）。
        self._context_limit = 0
        self._model_label = "model"
        self._chat_prompt: Any = None
        self._slash_commands: list[tuple[str, str]] = []
        # 当前活动的 Live spinner（render_stream 期间）。confirm 需要在提示前停掉它，
        # 否则 spinner 占着终端，确认输入提示不可见、无法输入 → 卡死。
        self._active_live: Any = None
        # 是否处于行首。流式正文用 end="" 打印会停在半行；confirm 提示前需据此补换行，
        # 否则提示与残留文本挤在同一行，导致输入被污染、选择被误判为拒绝。
        self._at_line_start = True

    def set_show_reasoning(self, value: bool) -> None:
        self._show_reasoning = value

    def set_context_limit(self, limit: int) -> None:
        self._context_limit = limit

    def set_model_label(self, model: str) -> None:
        self._model_label = model

    def set_slash_commands(self, commands: list[tuple[str, str]]) -> None:
        self._slash_commands = list(commands)
        if self._chat_prompt is not None:
            self._chat_prompt.set_commands(self._slash_commands)

    @property
    def display_mode(self) -> DisplayMode:
        return self._display_mode

    def set_display_mode(self, value: DisplayMode, *, force: bool = False) -> None:
        if force or not self._display_override:
            self._display_mode = value

    def show_run_id(self, run_id: str, *, force: bool = False) -> None:
        if force or self._display_mode == "verbose":
            self._console.print(f"Run ID：{run_id}", style="dim")

    def banner(
        self,
        provider_name: str,
        model: str,
        permission_mode: str,
        execution_backend: str,
        *,
        mcp_server_count: int = 0,
    ) -> None:
        self.set_model_label(model)
        if self._display_mode == "quiet":
            return
        self._console.print(
            build_banner(
                provider_name,
                model,
                os.getcwd(),
                permission_mode,
                execution_backend,
                mcp_server_count=mcp_server_count,
                verbose=self._display_mode == "verbose",
            )
        )

    @contextmanager
    def runtime_startup(self):
        """渲染 Runtime 创建阶段；返回 UI 无关 observer 回调。"""
        if self._display_mode == "quiet":
            yield lambda _event: None
            return
        with self._console.status("正在启动 Assistant Agent...", spinner="dots") as status:

            def observe(event: RuntimeStartupEvent) -> None:
                if event.status == "started" and event.message:
                    status.update(event.message)

            yield observe

    def user_echo(self, task: str) -> None:
        if self._display_mode == "quiet":
            return
        self._print_input_rule()
        line = Text("› ", style="bold cyan")
        line.append(task)
        self._console.print(line)
        self._print_input_rule(style="dim")

    def chat_input(self) -> str:
        """读取普通聊天输入，使用与任务回显一致的回合边界。"""
        self._console.print()
        if sys.stdin.isatty() and sys.stdout.isatty():
            value = self._read_chat_line()
            if value:
                self._print_chat_submission(value)
            return value
        self._print_input_rule()
        value = self._read_chat_line()
        self._print_input_rule(style="dim")
        return value

    def _read_chat_line(self) -> str:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return self.input("[bold cyan]› [/bold cyan]")

        from assistant_agent.ui.chat_prompt import ChatPrompt

        if self._chat_prompt is None:
            self._chat_prompt = ChatPrompt(self._slash_commands)
        return self._chat_prompt.read()

    def _print_input_rule(self, *, style: str = "cyan") -> None:
        width = max(self._console.width - 1, 1)
        self._console.print("─" * width, style=style)

    def _print_chat_submission(self, value: str) -> None:
        width = max(self._console.width - 1, 1)
        line = Text("› ", style="bold #b0b0b0 on #303030")
        line.append(value, style="#f0f0f0 on #303030")
        line.truncate(width, overflow="ellipsis", pad=True)
        self._console.print(line)

    def render_stream(self, events: Iterator[StepEvent]) -> None:
        """消费一次任务的流式事件并渲染。

        协调原则（见 M2 方案 7.4）：spinner（Live）只用于"无正文输出的空窗期"，
        一旦有正文/思考增量就停 Live、直接打印，两者在时间上错开，避免刷屏错位。
        """
        from assistant_agent.ui.conversation_renderer import ConversationRenderer

        ConversationRenderer(self, self._display_mode, self._show_reasoning).render(events)

    def confirm(self, message: str) -> ConfirmChoice:
        """危险操作确认，返回用户选择：allow / always / deny。

        关键：提示前必须停掉正在转的 Live spinner，否则 spinner 占着终端，
        确认提示不可见、无法输入 → 卡死（M2 流式引入的问题）。
        """
        suspend_active(self)
        # 若上一段流式正文停在半行，先补换行，保证提示与输入独占干净的新行，
        # 避免残留文本污染输入、导致选择被误判。
        if not self._at_line_start:
            self._console.print()
            self._at_line_start = True
        self._console.print("[bold yellow]确认执行[/bold yellow]")
        self._console.print(message, style="yellow", markup=False)
        self._console.print(
            "[green]1[/green] 允许  [cyan]2[/cyan] 本会话允许  [red]3[/red] 拒绝（默认）"
        )
        answer = self.input("[bold]选择 [1/2/3]: [/bold]").strip()
        choice: ConfirmChoice = "deny"
        if answer == "1":
            choice = "allow"
        elif answer == "2":
            choice = "always"
        if choice != "deny":
            activity = getattr(self, "_activity", None)
            if activity is not None:
                activity.resume("执行已授权操作")
            self._at_line_start = True
        return choice

    def confirm_scoped(self, message: str, broader_label: str) -> ConfirmChoice:
        """带上级会话 scope 的确认；公开 Web 使用短三选一文案。"""
        suspend_active(self)
        if not self._at_line_start:
            self._console.print()
            self._at_line_start = True
        self._console.print("[bold yellow]确认执行[/bold yellow]")
        self._console.print(message, style="yellow", markup=False)
        public_web = broader_label == "本会话允许当前联网工具访问公开网络"
        if public_web:
            self._console.print(
                "[green]1[/green] 仅本次  [cyan]2[/cyan] 本会话允许当前联网工具访问公开网络  "
                "[red]3[/red] 拒绝（默认）"
            )
            answer = self.input("[bold]选择 [1/2/3]: [/bold]").strip()
        else:
            self._console.print(
                "[green]1[/green] 允许一次  [cyan]2[/cyan] 本会话允许此工具  "
                f"[blue]3[/blue] {broader_label}  [red]4[/red] 拒绝（默认）"
            )
            answer = self.input("[bold]选择 [1/2/3/4]: [/bold]").strip()
        choice: ConfirmChoice = "deny"
        if answer == "1":
            choice = "allow"
        elif answer == "2" and public_web:
            choice = "broader"
        elif answer == "2":
            choice = "always"
        elif answer == "3":
            choice = "broader"
        if choice != "deny":
            activity = getattr(self, "_activity", None)
            if activity is not None:
                activity.resume("执行已授权操作")
            self._at_line_start = True
        return choice

    def info(self, text: str) -> None:
        if self._display_mode == "quiet":
            return
        self._console.print(f"[dim]{text}[/dim]")

    def command_info(self, text: str) -> None:
        """显示交互控制命令反馈；quiet 只隐藏 Agent 轨迹，不隐藏控制面。"""
        self._console.print(f"[dim]{text}[/dim]")

    def error(self, text: str) -> None:
        self._console.print(f"[bold red]{text}[/bold red]")

    def input(self, prompt: str = "") -> str:
        return read_input(prompt)  # 纯文本 prompt，避免 Linux 退格删提示符

    def confirm_continue(self, used: int) -> bool:
        """用尽轮数时询问是否继续。注入到 AgentLoop.continue_check。"""
        suspend_active(self)
        answer = (
            self.input(
                f"[bold yellow]已执行 {used} 轮仍未完成，继续吗？输入 y 继续: [/bold yellow]"
            )
            .strip()
            .lower()
        )
        allowed = answer in ("y", "yes")
        if allowed:
            activity = getattr(self, "_activity", None)
            if activity is not None:
                activity.resume("继续处理")
        return allowed

    def ask_question(self, question: str, options: list[str]) -> str:
        """层1 意图澄清：方向键菜单选择，返回用户所选（或"其他"的自由文本）。

        与 confirm 一样：提示前停掉 spinner、补换行，避免占屏/输入污染。
        优先用 questionary 的 ↑/↓ 选择菜单（Claude 风格）；终端不支持时回退到编号输入。
        """
        suspend_active(self)
        if not self._at_line_start:
            self._console.print()
            self._at_line_start = True

        other = "其他（自行输入）"
        try:
            import questionary

            selected = questionary.select(
                question,
                choices=[*options, other],
                use_shortcuts=True,  # 每项带数字快捷键，可按数字直接选
                instruction="（↑/↓ 或数字键选择，回车确认）",
            ).ask()  # Esc/Ctrl+C 返回 None
            self._at_line_start = True
            if selected is None:
                return ""  # 用户取消，交模型判断
            if selected == other:
                return questionary.text("请输入你的想法：").ask() or ""
            return selected
        except Exception:
            # 终端不支持交互菜单（或 prompt_toolkit 出错）→ 回退到编号输入
            return self._ask_question_fallback(question, options, other)

    def _ask_question_fallback(self, question: str, options: list[str], other: str) -> str:
        """编号输入兜底（questionary 不可用时）。"""
        self._console.print(f"[bold cyan]? {question}[/bold cyan]")
        for i, opt in enumerate(options, start=1):
            self._console.print(f"  [cyan]{i}[/cyan] {opt}")
        other_idx = len(options) + 1
        self._console.print(f"  [cyan]{other_idx}[/cyan] {other}")
        answer = self.input(f"[bold]请选择 [1-{other_idx}]: [/bold]").strip()
        self._at_line_start = True
        if answer.isdigit():
            idx = int(answer)
            if 1 <= idx <= len(options):
                return options[idx - 1]
            if idx == other_idx:
                return self.input("[bold]请输入你的想法: [/bold]").strip()
        return answer

    def print_providers(self, rows: list[tuple[str, str, str]]) -> None:
        """渲染 provider 列表。rows 为 (名字, 模型, 云端/本地)。"""
        self._console.print(build_providers_table(rows))

    def print_sessions(self, metas: list[Any]) -> None:
        """渲染历史会话列表。metas 为 SessionMeta 序列。"""
        self._console.print(build_sessions_table(metas))
