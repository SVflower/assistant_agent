"""工具活动的 normal/verbose/quiet 终端渲染。"""

from __future__ import annotations

from typing import Literal

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from assistant_agent.contracts.events import ItemEvent
from assistant_agent.observability.redaction import sanitize_for_display
from assistant_agent.tools.display import ToolPreview, call_display, safe_text

DisplayMode = Literal["normal", "verbose", "quiet"]

_CODE_BACKGROUND = "#16181d"
_DIFF_ADDED = "#d9f7e2 on #183c2b"
_DIFF_REMOVED = "#ffe1e4 on #4a2429"
_DIFF_HEADER = f"#8b949e on {_CODE_BACKGROUND}"
_DIFF_HUNK = f"#c9a7ff on {_CODE_BACKGROUND}"
_DIFF_CONTEXT = f"#d0d4dc on {_CODE_BACKGROUND}"


class ToolRenderer:
    def __init__(self, console: Console, mode: DisplayMode) -> None:
        self._console = console
        self._mode = mode

    def call(self, event: ItemEvent) -> str:
        display = event.display or call_display(event.tool_name, event.tool_args or {})
        action = safe_text(display.action, 80)
        target = safe_text(display.target, 160)
        label = action + (f" {target}" if target else "")
        if display.timeout_seconds is not None:
            label += f" · 最长 {display.timeout_seconds:g}s"
        if self._mode == "normal":
            if display.preview is not None:
                self._console.print(_call_line(action, target))
                self._print_preview(display.preview)
            elif display.importance == "external":
                self._console.print(_decision_line(action, target))
            return label
        if self._mode == "quiet":
            return label
        line = _call_line(action, target)
        suffix = event.tool_name
        if event.call_id:
            suffix += f" · {event.call_id[:12]}"
        line.append(f"  [{suffix}]", style="dim")
        self._console.print(line)
        if display.detail:
            detail = _indent(safe_text(display.detail, 800, multiline=True), "  ")
            self._console.print(Text(detail, style="dim"))
        return label

    def result(self, event: ItemEvent) -> None:
        display = event.display
        raw_summary = display.summary if display and display.summary else event.text
        summary = safe_text(raw_summary, 180)
        if self._mode == "quiet" and not event.is_error:
            return
        if self._mode == "normal":
            if display and display.preview is not None:
                marker = "x" if event.is_error else "✓"
                style = "bold red" if event.is_error else "green"
                line = Text(f"  {marker} ", style=style)
                line.append("失败：" if event.is_error else summary, style=style)
                if event.is_error:
                    line.append(summary, style="red")
            else:
                action = safe_text(display.action, 80) if display else "工具"
                target = safe_text(display.target, 160) if display else ""
                line = _call_line(action, target)
                line.append("  ·  ", style="dim")
                if event.is_error:
                    line.append("失败：", style="bold red")
                    line.append(summary, style="red")
                else:
                    line.append(summary, style="green")
            self._console.print(line)
        else:
            marker = "x" if event.is_error else "✓"
            style = "bold red" if event.is_error else "green"
            self._console.print(Text(f"  {marker} {summary}", style=style))
        detail = display.detail if display else event.text
        should_expand = event.is_error or self._mode == "verbose"
        if should_expand and detail and safe_text(detail, 180) != summary:
            limit = 1200 if self._mode == "verbose" else 500
            rendered = safe_text(detail, limit, multiline=True)
            self._console.print(Text(_indent(rendered, "    "), style="dim"))
        if self._mode == "verbose" and event.result_metadata:
            sanitized = sanitize_for_display(event.result_metadata, 200)
            metadata = safe_text(sanitized, 500)
            diagnostics = f"    code={event.result_code} metadata={metadata}"
            self._console.print(Text(diagnostics, style="dim"))

    def _print_preview(self, preview: ToolPreview) -> None:
        if preview.kind == "code":
            title = f"  准备写入 · {preview.total_lines} 行"
        else:
            title = f"  准备修改 · +{preview.added_lines} -{preview.removed_lines}"
        if preview.shown_lines < preview.total_lines:
            title += f" · 显示前 {preview.shown_lines} 行"
        self._console.print(Text(title, style="dim"))
        if not preview.content:
            return
        if preview.kind == "diff":
            self._console.print(_diff_table(preview.content))
            return
        self._console.print(
            Syntax(
                preview.content,
                preview.language,
                theme="ansi_dark",
                background_color=_CODE_BACKGROUND,
                line_numbers=True,
                word_wrap=False,
                padding=(0, 2),
            )
        )


def _indent(value: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in value.splitlines())


def _call_line(action: str, target: str) -> Text:
    line = Text("• ", style="cyan")
    line.append(action, style="bold")
    if target:
        line.append(" ")
        line.append(target, style="cyan")
    return line


def _decision_line(action: str, target: str) -> Text:
    line = Text("◆ ", style="yellow")
    line.append(f"准备{action}", style="bold yellow")
    if target:
        line.append(" ")
        line.append(target, style="cyan")
    return line


def _diff_table(content: str) -> Table:
    table = Table(
        box=None,
        show_header=False,
        show_edge=False,
        padding=(0, 2),
        expand=True,
        style=f"on {_CODE_BACKGROUND}",
    )
    table.add_column(overflow="fold")
    for value in content.splitlines():
        if value.startswith("+++") or value.startswith("---") or value.startswith("…"):
            style = _DIFF_HEADER
        elif value.startswith("@@"):
            style = _DIFF_HUNK
        elif value.startswith("+"):
            style = _DIFF_ADDED
        elif value.startswith("-"):
            style = _DIFF_REMOVED
        else:
            style = _DIFF_CONTEXT
        table.add_row(Text(value), style=style)
    return table
