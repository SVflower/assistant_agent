"""终端输入输出，基于 Rich。

负责把 AgentLoop 的 StepEvent 渲染给用户，并提供危险操作的确认交互。
"""

from __future__ import annotations

import json
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
            spinner.update(text=f"{text}（{_format_elapsed(time.monotonic() - start)}）")
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
                    args = _format_args(event.tool_args)
                    self._console.print(
                        f"\n[yellow]→ 调用工具[/yellow] [bold]{event.tool_name}[/bold]({args})"
                    )
                    self._at_line_start = True
                    spin("执行中…")
                elif event.kind == "tool_result":
                    stop_live()
                    style = "red" if event.is_error else "dim cyan"
                    preview = _truncate(event.text, 500)
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
        summary = f"耗时 {_format_elapsed(elapsed)}"
        if usage:
            summary += f" · token 用量：{_format_usage(usage)}"
        self._console.print(f"[dim]{summary}[/dim]")

    def confirm(self, message: str) -> str:
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
        if answer == "1":
            return "allow"
        if answer == "2":
            return "always"
        return "deny"

    def info(self, text: str) -> None:
        self._console.print(f"[dim]{text}[/dim]")

    def error(self, text: str) -> None:
        self._console.print(f"[bold red]{text}[/bold red]")


def _format_args(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    try:
        items = []
        for k, v in args.items():
            v_str = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            items.append(f"{k}={_truncate(v_str, 80)}")
        return ", ".join(items)
    except (TypeError, ValueError):
        return str(args)


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _format_usage(usage: dict[str, int]) -> str:
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens", 0)
    return f"↑{prompt} ↓{completion} 共 {total}"


def _format_elapsed(seconds: float) -> str:
    """人类可读的耗时：<60s 显示秒，否则分秒。"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"
