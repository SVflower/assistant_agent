"""终端输入输出，基于 Rich。

负责把 AgentLoop 的 StepEvent 渲染给用户，并提供危险操作的确认交互。
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from typing import Any

from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table

from assistant_agent.agent.loop import StepEvent
from assistant_agent.tools.base import ConfirmChoice
from assistant_agent.ui.formatting import (
    format_args,
    format_elapsed,
    format_usage,
    truncate,
)


class Console:
    """对 Rich 的薄封装，集中所有终端输出。"""

    def __init__(self, show_reasoning: bool = False) -> None:
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
        # 当前活动的 Live spinner（render_stream 期间）。confirm 需要在提示前停掉它，
        # 否则 spinner 占着终端，确认输入提示不可见、无法输入 → 卡死。
        self._active_live: Any = None
        # 是否处于行首。流式正文用 end="" 打印会停在半行；confirm 提示前需据此补换行，
        # 否则提示与残留文本挤在同一行，导致输入被污染、选择被误判为拒绝。
        self._at_line_start = True

    def set_show_reasoning(self, value: bool) -> None:
        self._show_reasoning = value

    def banner(self, provider_name: str, model: str) -> None:
        # 当前处理位置：工作目录
        cwd = os.getcwd()

        info = Table.grid(padding=(0, 1))
        info.add_column(justify="right", style="dim")
        info.add_column()
        info.add_row("Agent", "[bold]Assistant Agent[/bold]")
        info.add_row("provider", f"[cyan]{provider_name}[/cyan]")
        info.add_row("model", f"[cyan]{model}[/cyan]")
        info.add_row("位置", f"[green]{cwd}[/green]")

        self._console.print(Panel(info, border_style="blue", expand=False))

    def user_echo(self, task: str) -> None:
        self._console.print(f"[bold green]你:[/bold green] {task}")

    def render_stream(self, events: Iterator[StepEvent]) -> None:
        """消费一次任务的流式事件并渲染。

        协调原则（见 M2 方案 7.4）：spinner（Live）只用于"无正文输出的空窗期"，
        一旦有正文/思考增量就停 Live、直接打印，两者在时间上错开，避免刷屏错位。
        """
        from rich.live import Live

        start = time.monotonic()
        spinner = Spinner("dots", text="连接模型…")
        live: Live | None = Live(
            spinner, console=self._console, refresh_per_second=12, transient=True
        )
        live.start()
        live_active = True
        self._active_live = live
        streaming_text = False  # 是否正在打印正文（正文期间不开 spinner）
        final_streamed = False  # 最终回复是否已通过流式正文打印过（避免 final 重复整段）
        usage: dict[str, int] | None = None

        def stop_live() -> None:
            nonlocal live, live_active
            if live_active and live is not None:
                live.stop()
                live_active = False
                self._active_live = None

        def spin(text: str) -> None:
            """切回 spinner 状态（仅在非正文期），附带已耗时。"""
            nonlocal live, live_active
            if streaming_text:
                return
            spinner.update(text=f"{text}（{format_elapsed(time.monotonic() - start)}）")
            if not live_active:
                live = Live(
                    spinner, console=self._console, refresh_per_second=12, transient=True
                )
                live.start()
                live_active = True
            self._active_live = live

        try:
            for event in events:
                if event.kind == "reasoning":
                    if self._show_reasoning:
                        stop_live()
                        self._console.print(f"[dim italic]{event.text}[/dim italic]", end="")
                        self._at_line_start = False
                    else:
                        spin("思考中…")
                elif event.kind == "content_delta":
                    stop_live()
                    if not streaming_text:
                        streaming_text = True
                    final_streamed = True
                    self._console.print(event.text, end="", markup=False)
                    self._at_line_start = event.text.endswith("\n")
                elif event.kind == "tool_call":
                    stop_live()
                    streaming_text = False
                    final_streamed = False  # 工具后模型会再流一段新的最终回复
                    args = format_args(event.tool_args)
                    self._console.print(
                        f"\n[yellow]→ 调用工具[/yellow] [bold]{event.tool_name}[/bold]({args})"
                    )
                    self._at_line_start = True
                    spin("执行中…")
                elif event.kind == "tool_result":
                    stop_live()
                    style = "red" if event.is_error else "dim cyan"
                    preview = truncate(event.text, 500)
                    self._console.print(f"[{style}]  {preview}[/{style}]")
                    self._at_line_start = True
                    spin("思考中…")
                elif event.kind == "usage":
                    usage = event.usage
                elif event.kind == "final":
                    stop_live()
                    streaming_text = False
                    if final_streamed:
                        # 正文已流式打印过，不重复整段，只收尾换行。
                        self._console.print()
                        self._console.print("[dim green]— 完成 —[/dim green]")
                    else:
                        # 没有流式正文（如纯工具轮直接结束），补一个结果面板。
                        self._console.print()
                        self._console.print(Panel(event.text, title="结果", border_style="green"))
                    self._at_line_start = True
                elif event.kind == "error":
                    stop_live()
                    streaming_text = False
                    self._console.print()
                    self._console.print(Panel(event.text, title="错误", border_style="red"))
                    self._at_line_start = True
                elif event.kind == "interrupted":
                    stop_live()
                    streaming_text = False
                    self._console.print()
                    self._console.print(f"[yellow]⏹ {event.text}[/yellow]")
                    self._at_line_start = True
        finally:
            stop_live()

        elapsed = time.monotonic() - start
        summary = f"耗时 {format_elapsed(elapsed)}"
        if usage:
            summary += f" · token 用量：{format_usage(usage)}"
        self._console.print(f"[dim]{summary}[/dim]")

    def confirm(self, message: str) -> ConfirmChoice:
        """危险操作确认，返回用户选择：allow / always / deny。

        关键：提示前必须停掉正在转的 Live spinner，否则 spinner 占着终端，
        确认提示不可见、无法输入 → 卡死（M2 流式引入的问题）。
        """
        if self._active_live is not None:
            self._active_live.stop()
            self._active_live = None
        # 若上一段流式正文停在半行，先补换行，保证提示与输入独占干净的新行，
        # 避免残留文本污染输入、导致选择被误判。
        if not self._at_line_start:
            self._console.print()
            self._at_line_start = True
        self._console.print(f"[bold yellow]⚠ {message}[/bold yellow]")
        self._console.print(
            "  [green]1[/green] 允许    "
            "[cyan]2[/cyan] 允许且本次会话不再询问此类    "
            "[red]3[/red] 拒绝"
        )
        answer = self._console.input("[bold]请选择 [1/2/3]（默认 3 拒绝）: [/bold]").strip()
        choice: ConfirmChoice = "deny"
        if answer == "1":
            choice = "allow"
        elif answer == "2":
            choice = "always"
        if choice != "deny":
            # 放行后到命令真正跑完之间没有 spinner（confirm 已停掉它）。
            # 补一行静态反馈，避免慢命令期间看着像"卡住无反应"。
            self._console.print("[dim]▶ 执行中，请稍候…[/dim]")
            self._at_line_start = True
        return choice

    def info(self, text: str) -> None:
        self._console.print(f"[dim]{text}[/dim]")

    def error(self, text: str) -> None:
        self._console.print(f"[bold red]{text}[/bold red]")

    def input(self, prompt: str) -> str:
        """读取一行用户输入（收口对底层 console 的访问）。"""
        return self._console.input(prompt)

    def ask_question(self, question: str, options: list[str]) -> str:
        """层1 意图澄清：方向键菜单选择，返回用户所选（或"其他"的自由文本）。

        与 confirm 一样：提示前停掉 spinner、补换行，避免占屏/输入污染。
        优先用 questionary 的 ↑/↓ 选择菜单（Claude 风格）；终端不支持时回退到编号输入。
        """
        if self._active_live is not None:
            self._active_live.stop()
            self._active_live = None
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
        table = Table(title="可用 provider", border_style="blue")
        table.add_column("provider", style="cyan", no_wrap=True)
        table.add_column("模型")
        table.add_column("类型", style="dim")
        for name, model, kind in rows:
            table.add_row(name, model, kind)
        self._console.print(table)

    def print_sessions(self, metas: list[Any]) -> None:
        """渲染历史会话列表。metas 为 SessionMeta 序列。"""
        table = Table(title="历史会话", show_lines=False, border_style="blue")
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("更新时间", style="dim")
        table.add_column("消息数", justify="right")
        table.add_column("首条内容")
        for m in metas:
            table.add_row(m.id, m.updated_at, str(m.message_count), m.preview)
        self._console.print(table)
