"""终端输入输出，基于 Rich。

负责把 AgentLoop 的 StepEvent 渲染给用户，并提供危险操作的确认交互。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.table import Table

from assistant_agent.agent.loop import StepEvent


class Console:
    """对 Rich 的薄封装，集中所有终端输出。"""

    def __init__(self) -> None:
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

    def render_event(self, event: StepEvent) -> None:
        if event.kind == "assistant":
            self._console.print(f"[dim]{event.text}[/dim]")
        elif event.kind == "tool_call":
            args = _format_args(event.tool_args)
            self._console.print(
                f"[yellow]→ 调用工具[/yellow] [bold]{event.tool_name}[/bold]({args})"
            )
        elif event.kind == "tool_result":
            style = "red" if event.is_error else "dim cyan"
            preview = _truncate(event.text, 500)
            self._console.print(f"[{style}]  {preview}[/{style}]")
        elif event.kind == "final":
            self._console.print(Panel(event.text, title="结果", border_style="green"))
        elif event.kind == "error":
            self._console.print(Panel(event.text, title="错误", border_style="red"))

    def confirm(self, message: str) -> bool:
        """危险操作确认。注入到 ToolContext.confirm。"""
        self._console.print(f"[bold red]⚠ {message}[/bold red]")
        answer = self._console.input("[bold]输入 y 允许，其他键拒绝: [/bold]").strip().lower()
        return answer in ("y", "yes")

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
