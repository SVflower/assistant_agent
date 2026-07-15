"""工具活动的 normal/verbose/quiet 终端渲染。"""

from __future__ import annotations

from typing import Literal

from rich.console import Console
from rich.text import Text

from assistant_agent.agent.events import StepEvent
from assistant_agent.obs.redaction import sanitize_for_display
from assistant_agent.tools.display import call_display, safe_text

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
        if self._mode == "quiet":
            return label
        line = Text("• ", style="yellow")
        line.append(label, style="bold")
        if self._mode == "verbose":
            suffix = event.tool_name
            if event.call_id:
                suffix += f" · {event.call_id[:12]}"
            line.append(f"  [{suffix}]", style="dim")
        self._console.print(line)
        if self._mode == "verbose" and display.detail:
            detail = _indent(safe_text(display.detail, 800, multiline=True), "  ")
            self._console.print(Text(detail, style="dim"))
        return label

    def result(self, event: StepEvent) -> None:
        display = event.display
        raw_summary = display.summary if display and display.summary else event.text
        summary = safe_text(raw_summary, 180)
        if self._mode == "quiet" and not event.is_error:
            return
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


def _indent(value: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in value.splitlines())
