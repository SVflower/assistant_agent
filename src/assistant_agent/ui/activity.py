"""Single-live terminal activity feedback with a dynamically rendered timer."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from assistant_agent.ui.formatting import format_elapsed

_LONG_WAIT_SECONDS = 8.0


class ActivityIndicator:
    """Rich renderable whose elapsed time advances independently of Agent events."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._action = "等待模型响应"
        self._target = ""
        self._spinner = Spinner("dots", style="cyan")
        self._lock = threading.Lock()

    def update(self, action: str, target: str = "") -> None:
        with self._lock:
            if (action, target) != (self._action, self._target):
                self._started = self._clock()
            self._action = action
            self._target = target

    def __rich_console__(self, _console: Console, _options: ConsoleOptions) -> RenderResult:
        now = self._clock()
        with self._lock:
            label = self._action + (f" {self._target}" if self._target else "")
            started = self._started
        elapsed = max(now - started, 0.0)
        text = Text(f"{label}  {format_elapsed(elapsed)}", style="dim")
        if elapsed >= _LONG_WAIT_SECONDS:
            text.append("  · 仍在等待 · Ctrl+C 可暂停", style="yellow")
        self._spinner.update(text=text)
        yield self._spinner.render(now)


class ActivityController:
    """Own one Rich Live object for all waiting phases in a task turn."""

    def __init__(
        self,
        console: Console,
        *,
        enabled: bool | None = None,
        on_live: Callable[[Any | None], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        live_factory: Callable[..., Any] = Live,
    ) -> None:
        terminal = console.is_terminal and os.environ.get("TERM", "").lower() != "dumb"
        self.enabled = terminal if enabled is None else enabled
        self._on_live = on_live or (lambda _live: None)
        self._indicator = ActivityIndicator(clock=clock)
        self._live = (
            live_factory(
                self._indicator,
                console=console,
                refresh_per_second=8,
                transient=True,
            )
            if self.enabled
            else None
        )
        self._active = False
        self._closed = False

    @property
    def indicator(self) -> ActivityIndicator:
        return self._indicator

    @property
    def active(self) -> bool:
        return self._active

    def show(self, action: str, target: str = "") -> None:
        if self._closed:
            return
        self._indicator.update(action, target)
        if self._live is not None and not self._active:
            self._live.start(refresh=True)
            self._active = True
            self._on_live(self._live)

    def suspend(self) -> None:
        if self._live is not None and self._active:
            self._live.stop()
            self._active = False
            self._on_live(None)

    def resume(self, action: str | None = None, target: str = "") -> None:
        if action is not None:
            self.show(action, target)
        elif not self._closed and self._live is not None and not self._active:
            self._live.start(refresh=True)
            self._active = True
            self._on_live(self._live)

    def complete(self) -> None:
        self.suspend()
        self._closed = True


def suspend_active(owner: Any) -> None:
    """Stop Activity/other Live output before terminal input takes ownership."""
    activity = getattr(owner, "_activity", None)
    if activity is not None:
        activity.suspend()
    active_live = getattr(owner, "_active_live", None)
    if active_live is not None:
        active_live.stop()
        owner._active_live = None
