"""StepEvent 到终端会话轨迹的渲染状态机。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner

from assistant_agent.agent.events import StepEvent
from assistant_agent.tools.display import safe_text
from assistant_agent.ui.formatting import build_response_panel, build_turn_status, format_elapsed
from assistant_agent.ui.markdown_stream import StreamingMarkdownRenderer
from assistant_agent.ui.tool_renderer import DisplayMode, ToolRenderer


class ConversationRenderer:
    def __init__(self, owner: Any, mode: DisplayMode, show_reasoning: bool) -> None:
        self._owner = owner
        self._console = owner._console
        self._mode = mode
        self._show_reasoning = show_reasoning

    def render(self, events: Iterator[StepEvent]) -> None:
        from rich.live import Live

        start = time.monotonic()
        spinner = Spinner("dots", text="连接模型…")
        live: Live | None = None
        live_active = False
        markdown: StreamingMarkdownRenderer | None = None
        final_streamed = False
        had_tool = False
        total_in = total_out = last_prompt = 0
        got_usage = False

        def bind_live(value: Any | None) -> None:
            self._owner._active_live = value

        def stop_live() -> None:
            nonlocal live, live_active
            if live_active and live is not None:
                live.stop()
            live = None
            live_active = False
            bind_live(None)

        def stop_markdown(*, commit: bool = True) -> None:
            nonlocal markdown
            if markdown is not None:
                markdown.finish(commit=commit)
                markdown = None
            self._owner._at_line_start = True

        def spin(text: str) -> None:
            nonlocal live, live_active
            if self._mode == "quiet" or markdown is not None:
                return
            spinner.update(text=f"{text}（{format_elapsed(time.monotonic() - start)}）")
            if not live_active:
                live = Live(spinner, console=self._console, refresh_per_second=12, transient=True)
                live.start()
                live_active = True
                bind_live(live)

        tool_renderer = ToolRenderer(self._console, self._mode)
        spin("连接模型…")
        try:
            for event in events:
                if event.kind == "reasoning":
                    if self._show_reasoning and self._mode != "quiet":
                        stop_live()
                        reasoning = safe_text(event.text, 0, multiline=True)
                        self._console.print(reasoning, style="dim italic", markup=False, end="")
                        self._owner._at_line_start = event.text.endswith("\n")
                    else:
                        spin("思考中…")
                elif event.kind == "content_delta":
                    stop_live()
                    if self._mode != "quiet":
                        if markdown is None:
                            markdown = StreamingMarkdownRenderer(
                                self._console,
                                bind_live,
                                transient=self._mode == "normal",
                            )
                        markdown.append(event.text)
                    final_streamed = True
                elif event.kind == "tool_call":
                    stop_live()
                    keep_progress = self._mode == "verbose" or (
                        self._mode == "normal" and not had_tool
                    )
                    stop_markdown(commit=keep_progress)
                    had_tool = True
                    final_streamed = False
                    label = tool_renderer.call(event)
                    spin(f"{label}…")
                elif event.kind == "tool_result":
                    stop_live()
                    tool_renderer.result(event)
                    spin("思考中…")
                elif event.kind == "usage" and event.usage:
                    got_usage = True
                    total_in += event.usage.get("prompt_tokens", 0)
                    total_out += event.usage.get("completion_tokens", 0)
                    last_prompt = event.usage.get("prompt_tokens", last_prompt)
                elif event.kind == "final":
                    stop_live()
                    if self._mode == "quiet":
                        self._console.print(safe_text(event.text, 0, multiline=True), markup=False)
                    elif self._mode == "normal":
                        stop_markdown(commit=False)
                        final_text = safe_text(event.text, 0, multiline=True)
                        self._console.print(build_response_panel(final_text))
                    elif final_streamed:
                        stop_markdown()
                    else:
                        self._console.print(Markdown(safe_text(event.text, 0, multiline=True)))
                    self._owner._at_line_start = True
                elif event.kind == "error":
                    stop_live()
                    stop_markdown(commit=self._mode == "verbose")
                    error = safe_text(event.text, 1200, multiline=True)
                    self._console.print(Panel(error, title="错误", border_style="red"))
                    self._owner._at_line_start = True
                elif event.kind == "interrupted":
                    stop_live()
                    stop_markdown(commit=self._mode == "verbose")
                    interrupted = safe_text(event.text, 800, multiline=True)
                    self._console.print(f"已中断：{interrupted}", style="yellow", markup=False)
                    self._owner._at_line_start = True
                elif event.kind == "notice" and self._mode != "quiet":
                    stop_live()
                    stop_markdown(commit=self._mode == "verbose")
                    notice = safe_text(event.text, 800, multiline=True)
                    self._console.print(notice, style="dim", markup=False)
                    self._owner._at_line_start = True
        finally:
            stop_live()
            stop_markdown()

        if self._mode != "quiet":
            elapsed = format_elapsed(time.monotonic() - start)
            if got_usage:
                self._console.print(
                    build_turn_status(
                        self._owner._model_label,
                        elapsed,
                        total_in,
                        total_out,
                        last_prompt,
                        self._owner._context_limit,
                        self._console.width,
                    )
                )
            else:
                self._console.print(f"耗时 {elapsed}", style="dim")
