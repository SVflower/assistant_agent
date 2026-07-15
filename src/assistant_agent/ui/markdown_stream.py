"""基于 Rich Live 的流式 Markdown 渲染，失败时无损降级。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

from assistant_agent.tools.display import safe_text


class StreamingMarkdownRenderer:
    def __init__(self, console: Console, on_live: Callable[[Any | None], None]) -> None:
        self._console = console
        self._on_live = on_live
        self._buffer = ""
        self._live: Live | None = None
        self._failed = False

    @property
    def has_content(self) -> bool:
        return bool(self._buffer)

    def append(self, text: str) -> None:
        self._buffer += text
        if not self._console.is_terminal or self._failed:
            return
        try:
            renderable = Markdown(self._render_text())
            if self._live is None:
                self._live = Live(
                    renderable,
                    console=self._console,
                    refresh_per_second=12,
                    transient=False,
                    vertical_overflow="visible",
                )
                self._live.start()
                self._on_live(self._live)
            else:
                self._live.update(renderable, refresh=True)
        except Exception:
            self._failed = True
            self._stop_live()

    def finish(self) -> None:
        if not self._buffer:
            return
        if self._live is not None:
            try:
                self._live.update(Markdown(self._render_text()), refresh=True)
            except Exception:
                self._failed = True
            self._stop_live()
            if not self._failed:
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
