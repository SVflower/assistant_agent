"""线程安全的单次任务暂停与取消控制。"""

from __future__ import annotations

import threading

from assistant_agent.agent.run.ports import ControlState


class RunInterrupted(RuntimeError):
    def __init__(self, *, cancelled: bool) -> None:
        self.cancelled = cancelled
        super().__init__("任务已强制取消" if cancelled else "任务已暂停")


class RunControl:
    """进程内运行控制；请求只能从 running 单向升级到 pause/cancel。"""

    def __init__(self) -> None:
        self._state = ControlState.RUNNING
        self._lock = threading.Lock()
        self._changed = threading.Event()

    @property
    def state(self) -> ControlState:
        with self._lock:
            return self._state

    @property
    def pause_requested(self) -> bool:
        return self.state >= ControlState.PAUSE_REQUESTED

    @property
    def cancel_requested(self) -> bool:
        return self.state >= ControlState.CANCEL_REQUESTED

    def request_pause(self) -> ControlState:
        return self._upgrade(ControlState.PAUSE_REQUESTED)

    def request_cancel(self) -> ControlState:
        return self._upgrade(ControlState.CANCEL_REQUESTED)

    def request_interrupt(self) -> ControlState:
        """第一次请求暂停，后续请求升级为强制取消。"""
        with self._lock:
            target = (
                ControlState.PAUSE_REQUESTED
                if self._state is ControlState.RUNNING
                else ControlState.CANCEL_REQUESTED
            )
            self._state = max(self._state, target)
            self._changed.set()
            return self._state

    def reset(self) -> None:
        with self._lock:
            self._state = ControlState.RUNNING
            self._changed.clear()

    def wait(self, timeout: float) -> bool:
        return self._changed.wait(timeout)

    def _upgrade(self, target: ControlState) -> ControlState:
        with self._lock:
            self._state = max(self._state, target)
            self._changed.set()
            return self._state
