"""工具活动的 normal/verbose/quiet 终端渲染。"""

from __future__ import annotations

from typing import Literal

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from assistant_agent.agent.events import StepEvent
from assistant_agent.obs.redaction import sanitize_for_display
from assistant_agent.tools.display import ToolPreview, call_display, safe_text

DisplayMode = Literal["normal", "verbose", "quiet"]


class ToolRenderer:
    def __init__(self, console: Console, mode: DisplayMode) -> None:
        self._console = console
        self._mode = mode

    def call(self, event: StepEvent) -> str:
        display = event.display or call_display(event.tool_name, event.tool_args or {})
        action = safe_text(display.action, 80)
        target = safe_text(display.target, 160)
        label = action + (f" {target}" if target else "")
        if self._mode == "normal":
            if display.preview is not None:
                self._console.print(_call_line(action, target))
                self._print_preview(display.preview)
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

    def result(self, event: StepEvent) -> None:
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
            title = f"  写入预览 · {preview.total_lines} 行"
        else:
            title = f"  拟议变更 · +{preview.added_lines} -{preview.removed_lines}"
        if preview.shown_lines < preview.total_lines:
            title += f" · 显示前 {preview.shown_lines} 行"
        self._console.print(Text(title, style="dim"))
        if not preview.content:
            return
        self._console.print(
            Syntax(
                preview.content,
                preview.language,
                theme="ansi_dark",
                background_color="default",
                line_numbers=preview.kind == "code",
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
