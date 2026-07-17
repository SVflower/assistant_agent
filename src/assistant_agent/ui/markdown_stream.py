"""基于 Rich Live 的流式 Markdown 渲染，失败时无损降级。"""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from assistant_agent.tools.display import safe_text
from assistant_agent.ui.formatting import format_elapsed

_IDLE_FEEDBACK_SECONDS = 1.0
_LONG_WAIT_SECONDS = 8.0


class StreamingMarkdownView:
    """渲染流式正文，并在模型流暂时停更时追加活性反馈。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._text = ""
        self._updated_at = clock()
        self._spinner = Spinner("dots", style="cyan")
        self._lock = Lock()

    def touch(self, now: float) -> None:
        with self._lock:
            self._updated_at = now

    def update(self, text: str, *, now: float | None = None) -> None:
        with self._lock:
            self._text = text
            self._updated_at = self._clock() if now is None else now

    def __rich_console__(self, _console: Console, _options: ConsoleOptions) -> RenderResult:
        now = self._clock()
        with self._lock:
            text = self._text
            idle = max(now - self._updated_at, 0.0)
        yield Markdown(text)
        if idle < _IDLE_FEEDBACK_SECONDS:
            return
        status = Text(f"模型仍在生成  {format_elapsed(idle)}", style="dim")
        if idle >= _LONG_WAIT_SECONDS:
            status.append("  · 仍在等待 · Ctrl+C 可暂停", style="yellow")
        self._spinner.update(text=status)
        yield self._spinner.render(now)


class StreamingMarkdownRenderer:
    def __init__(
        self,
        console: Console,
        on_live: Callable[[Any | None], None],
        *,
        transient: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console
        self._on_live = on_live
        self._transient = transient
        self._buffer = ""
        self._live: Live | None = None
        self._failed = False
        self._last_render = 0.0
        self._refresh_interval = 1 / 15
        self._clock = clock
        self._view = StreamingMarkdownView(clock=clock)

    @property
    def has_content(self) -> bool:
        return bool(self._buffer)

    def append(self, text: str) -> None:
        self._buffer += text
        if not self._console.is_terminal or self._failed:
            return
        now = self._clock()
        self._view.touch(now)
        if self._live is not None and now - self._last_render < self._refresh_interval:
            return
        try:
            self._view.update(self._render_text(), now=now)
            if self._live is None:
                self._live = Live(
                    self._view,
                    console=self._console,
                    refresh_per_second=8,
                    transient=self._transient,
                    vertical_overflow="visible",
                )
                self._live.start()
                self._on_live(self._live)
            else:
                self._live.update(self._view, refresh=False)
            self._last_render = now
        except Exception:
            self._failed = True
            self._stop_live()

    def finish(self, *, commit: bool = True) -> None:
        if not self._buffer:
            return
        if self._live is not None:
            if not commit:
                self._stop_live()
                return
            try:
                self._live.update(Markdown(self._render_text()), refresh=True)
            except Exception:
                self._failed = True
            self._stop_live()
            if not self._failed and not self._transient:
                return
        elif not commit:
            return
        try:
            if self._failed:
                self._console.print(self._render_text(), markup=False)
            else:
                self._console.print(Markdown(self._render_text()))
        except Exception:
            self._console.print(self._render_text(), markup=False)

    def _render_text(self) -> str:
        return safe_text(self._buffer, 0, multiline=True)

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._on_live(None)
