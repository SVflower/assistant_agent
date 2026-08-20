"""RunCoordinator 使用的有界、可恢复运行观测记录器。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Literal

from assistant_agent.contracts.observability import (
    MAX_TRAJECTORY_ENTRIES,
    ContextUsageSnapshot,
    ModelUsageSnapshot,
    RunObservabilitySnapshot,
    TaskPlanItem,
    TaskPlanSnapshot,
    TimingSnapshot,
    TrajectoryCategory,
    TrajectoryEntry,
    TrajectoryStatus,
)

_FINISHED = {"paused", "completed", "failed", "cancelled"}


def new_observability(run_id: str, timestamp: str) -> RunObservabilitySnapshot:
    entry = TrajectoryEntry(
        entry_id=_entry_id(run_id, 1),
        sequence=1,
        category="run",
        status="started",
        title="Run started",
        started_at=timestamp,
    )
    return RunObservabilitySnapshot(
        timing=TimingSnapshot(
            run_started_at=timestamp,
            run_duration_ms=0,
            source="derived",
        ),
        context=ContextUsageSnapshot(),
        model_usage=ModelUsageSnapshot(),
        trajectory=(entry,),
    )


class RunObservabilityRecorder:
    """只接收安全生命周期事实；monotonic 基准不进入 checkpoint。"""

    def __init__(
        self,
        run_id: str,
        snapshot: RunObservabilitySnapshot,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.run_id = run_id
        self._snapshot = snapshot
        self._entries = list(snapshot.trajectory)
        self._monotonic = monotonic
        self._last_tick = monotonic()
        self._active: dict[str, tuple[str, float | None]] = {}
        self._phase_spans: dict[str, tuple[str, float | None]] = {}
        for entry in self._entries:
            if entry.status not in _FINISHED:
                if entry.category == "run" and entry.result_code in {
                    "preparing_context",
                    "syncing_session",
                }:
                    self._phase_spans[entry.result_code] = (entry.entry_id, None)
                    continue
                self._active[self._key(entry.category, entry.call_id)] = (entry.entry_id, None)
        usage = snapshot.model_usage
        self._model_base_input = usage.input_tokens or 0
        self._model_base_output = usage.output_tokens or 0
        self._model_base_cache_read = usage.cache_read_tokens
        self._model_base_cache_write = usage.cache_write_tokens
        self._prior_cache_unavailable = (
            usage.token_source == "provider" and usage.cache_source == "unavailable"
        )
        self._current_model_usage: dict[str, int] | None = None
        self._model_first_token_mono: float | None = None
        self._model_reported_duration_ms: int | None = None
        self._model_reported_ttft_ms: int | None = None
        self._checkpoint_bytes_known = (
            snapshot.orchestration.checkpoint_count in {None, 0}
            or snapshot.orchestration.checkpoint_bytes is not None
        )
        self._last_checkpoint_duration_ms: int | None = None

    @property
    def snapshot_value(self) -> RunObservabilitySnapshot:
        return self._snapshot

    @property
    def latest_entry(self) -> TrajectoryEntry | None:
        return self._entries[-1] if self._entries else None

    def start_model(self, timestamp: str) -> None:
        self._finish_category("model", "completed", timestamp)
        self._model_base_input = self._snapshot.model_usage.input_tokens or 0
        self._model_base_output = self._snapshot.model_usage.output_tokens or 0
        self._model_base_cache_read = self._snapshot.model_usage.cache_read_tokens
        self._model_base_cache_write = self._snapshot.model_usage.cache_write_tokens
        self._prior_cache_unavailable = (
            self._snapshot.model_usage.token_source == "provider"
            and self._snapshot.model_usage.cache_source == "unavailable"
        )
        self._current_model_usage = None
        self._model_first_token_mono = None
        self._model_reported_duration_ms = None
        self._model_reported_ttft_ms = None
        self._start("model", "Model call", timestamp)

    def first_model_signal(self, timestamp: str) -> bool:
        active = self._active.get(self._key("model", None))
        if active is None or self._model_first_token_mono is not None:
            return False
        self._model_first_token_mono = self._monotonic()
        self._replace_entry(
            active[0],
            status="streaming",
            title="Model streaming",
        )
        started = active[1]
        if started is not None:
            self._set_timing(
                first_token_latency_ms=_elapsed_ms(started, self._model_first_token_mono)
            )
        return True

    def observe_usage(self, usage: Mapping[str, int]) -> bool:
        prompt = _nonnegative(usage.get("prompt_tokens"))
        completion = _nonnegative(usage.get("completion_tokens"))
        if prompt is None and completion is None:
            return False
        current: dict[str, int] = {
            "input_tokens": prompt or 0,
            "output_tokens": completion or 0,
        }
        for source, target in (
            ("cache_read_tokens", "cache_read_tokens"),
            ("cache_write_tokens", "cache_write_tokens"),
        ):
            value = _nonnegative(usage.get(source))
            if value is not None:
                current[target] = value
        self._current_model_usage = current
        self._model_reported_duration_ms = _nonnegative(usage.get("model_duration_ms"))
        self._model_reported_ttft_ms = _nonnegative(usage.get("first_token_latency_ms"))
        input_tokens = self._model_base_input + current["input_tokens"]
        output_tokens = self._model_base_output + current["output_tokens"]
        cache_read = (
            None
            if self._prior_cache_unavailable
            else _sum_optional(self._model_base_cache_read, current.get("cache_read_tokens"))
        )
        cache_write = (
            None
            if self._prior_cache_unavailable
            else _sum_optional(self._model_base_cache_write, current.get("cache_write_tokens"))
        )
        cache_known = cache_read is not None or cache_write is not None
        cache_hit = (
            min(100.0, cache_read / input_tokens * 100)
            if cache_read is not None and input_tokens > 0
            else None
        )
        model_usage = ModelUsageSnapshot(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cache_hit_percent=cache_hit,
            token_source="provider",
            cache_source="provider" if cache_known else "unavailable",
            performance_source=self._snapshot.model_usage.performance_source,
        )
        used = current["input_tokens"]
        context = self._snapshot.context.model_copy(
            update={
                "used_tokens": used,
                "percent": _percent(used, self._snapshot.context.limit_tokens),
                "source": "provider",
            }
        )
        self._snapshot = self._snapshot.model_copy(
            update={"model_usage": model_usage, "context": context}
        )
        return True

    def finish_model(self, timestamp: str, status: TrajectoryStatus = "completed") -> None:
        if self._key("model", None) not in self._active:
            return
        duration = self._finish_category("model", status, timestamp)
        reported_duration = self._model_reported_duration_ms
        model_duration = reported_duration if reported_duration is not None else duration
        ttft = self._model_reported_ttft_ms or self._snapshot.timing.first_token_latency_ms
        current_output = (
            self._current_model_usage.get("output_tokens", 0)
            if self._current_model_usage is not None
            else None
        )
        decode_ms = (
            model_duration - ttft
            if model_duration is not None and ttft is not None and model_duration > ttft
            else None
        )
        rate = (
            current_output / (decode_ms / 1000)
            if current_output is not None and current_output > 0 and decode_ms
            else None
        )
        timing = self._snapshot.timing
        total_model = _add_duration(timing.model_duration_ms, model_duration)
        self._snapshot = self._snapshot.model_copy(
            update={
                "timing": timing.model_copy(
                    update={
                        "model_duration_ms": total_model,
                        "first_token_latency_ms": ttft,
                        "tokens_per_second": rate,
                        "source": "derived",
                    }
                ),
                "model_usage": self._snapshot.model_usage.model_copy(
                    update={"performance_source": "derived" if rate is not None else "unavailable"}
                ),
            }
        )

    def start_tool(self, call_id: str, tool_name: str, timestamp: str) -> None:
        self.finish_interactions(timestamp)
        self._start(
            "tool",
            f"Tool: {tool_name}",
            timestamp,
            call_id=call_id,
            tool_name=tool_name,
        )

    def finish_tool(
        self,
        call_id: str,
        timestamp: str,
        *,
        failed: bool,
        result_code: str | None,
    ) -> None:
        duration = self._finish(
            self._key("tool", call_id),
            "failed" if failed else "completed",
            timestamp,
            result_code=result_code,
        )
        if duration is not None:
            timing = self._snapshot.timing
            self._snapshot = self._snapshot.model_copy(
                update={
                    "timing": timing.model_copy(
                        update={
                            "tool_duration_ms": _add_duration(timing.tool_duration_ms, duration)
                        }
                    )
                }
            )

    def start_interaction(self, key: str, title: str, timestamp: str) -> None:
        self._start("interaction", title, timestamp, call_id=key, status="waiting")

    def finish_interactions(self, timestamp: str, status: TrajectoryStatus = "completed") -> None:
        keys = [key for key in self._active if key.startswith("interaction:")]
        for key in keys:
            duration = self._finish(key, status, timestamp)
            if duration is not None:
                timing = self._snapshot.timing
                self._snapshot = self._snapshot.model_copy(
                    update={
                        "timing": timing.model_copy(
                            update={
                                "interaction_wait_duration_ms": _add_duration(
                                    timing.interaction_wait_duration_ms, duration
                                )
                            }
                        )
                    }
                )

    def record_output(self, call_id: str, timestamp: str, result_code: str | None) -> None:
        self._instant(
            "output",
            "Output created",
            timestamp,
            call_id=call_id,
            result_code=result_code,
        )

    def record_output_validation(
        self, call_id: str, timestamp: str, *, passed: bool, result_code: str
    ) -> None:
        self._instant(
            "output",
            "Output validation passed" if passed else "Output validation failed",
            timestamp,
            call_id=call_id,
            result_code=result_code,
            status="completed" if passed else "failed",
        )

    def replace_task_plan(
        self, items: tuple[TaskPlanItem, ...], timestamp: str
    ) -> TaskPlanSnapshot:
        current = self._snapshot.task_plan
        snapshot = TaskPlanSnapshot(
            revision=1 if current is None else current.revision + 1,
            updated_at=timestamp,
            items=items,
        )
        self._snapshot = self._snapshot.model_copy(update={"task_plan": snapshot})
        return snapshot

    def complete_active_task_plan_item(self, timestamp: str) -> TaskPlanSnapshot | None:
        """成功终态收口唯一活动步骤；未开始步骤仍保留 pending 事实。"""
        current = self._snapshot.task_plan
        if current is None or not any(item.status == "in_progress" for item in current.items):
            return current
        snapshot = TaskPlanSnapshot(
            revision=current.revision + 1,
            updated_at=timestamp,
            items=tuple(
                item.model_copy(update={"status": "completed"})
                if item.status == "in_progress"
                else item
                for item in current.items
            ),
        )
        self._snapshot = self._snapshot.model_copy(update={"task_plan": snapshot})
        return snapshot

    def record_phase(self, phase: str, timestamp: str) -> None:
        if phase == "waiting_interaction":
            self.start_interaction("runtime", "Waiting for interaction", timestamp)
        elif phase == "preparing_context":
            self.finish_interactions(timestamp)
            self._start_phase(phase, "Preparing context", timestamp)
        elif phase == "calling_model":
            self._finish_phase("preparing_context", timestamp)
        elif phase == "syncing_session":
            self.finish_interactions(timestamp)
            self._start_phase(phase, "Syncing session", timestamp)
        elif phase == "saving_checkpoint":
            self.finish_interactions(timestamp)
            self._instant(
                "run",
                "Saving checkpoint",
                timestamp,
                result_code=phase,
                duration_ms=self._last_checkpoint_duration_ms,
            )

    def begin_checkpoint(self) -> None:
        current = self._snapshot.orchestration
        self._set_orchestration(
            checkpoint_count=(current.checkpoint_count or 0) + 1,
            source="derived",
        )

    def rollback_checkpoint(self) -> None:
        current = self._snapshot.orchestration
        count = current.checkpoint_count or 0
        self._set_orchestration(checkpoint_count=max(0, count - 1) or None)

    def record_checkpoint(self, duration_ms: int, payload_bytes: int | None) -> None:
        current = self._snapshot.orchestration
        if payload_bytes is None:
            self._checkpoint_bytes_known = False
        total_bytes = (
            (current.checkpoint_bytes or 0) + payload_bytes
            if self._checkpoint_bytes_known and payload_bytes is not None
            else None
        )
        self._set_orchestration(
            checkpoint_duration_ms=(current.checkpoint_duration_ms or 0) + duration_ms,
            checkpoint_bytes=total_bytes,
            source="derived",
        )
        self._last_checkpoint_duration_ms = duration_ms

    def finish_session_sync(self, timestamp: str) -> None:
        self._finish_phase("syncing_session", timestamp)

    def record_session_sync(self, duration_ms: int) -> None:
        current = self._snapshot.orchestration.session_sync_duration_ms
        self._set_orchestration(
            session_sync_duration_ms=(current or 0) + duration_ms,
            source="derived",
        )

    def current_snapshot(self) -> RunObservabilitySnapshot:
        return self._snapshot.model_copy(update={"trajectory": tuple(self._entries)})

    def update_estimated_context(self, report: Mapping[str, int]) -> None:
        projected = _nonnegative(report.get("used"))
        limit = _nonnegative(report.get("total"))
        current = self._snapshot.context
        source = current.source
        used = current.used_tokens
        if used is None:
            used = projected
            source = "estimated" if projected is not None else "unavailable"
        self._snapshot = self._snapshot.model_copy(
            update={
                "context": ContextUsageSnapshot(
                    used_tokens=used,
                    projected_tokens=projected,
                    limit_tokens=limit if limit and limit > 0 else None,
                    percent=_percent(used, limit),
                    source=source,
                )
            }
        )

    def pause(self, timestamp: str) -> None:
        self._finish_open_spans("paused", timestamp)

    def resume(self, timestamp: str) -> None:
        self._finish_open_spans("paused", timestamp)
        self._start("run", "Run resumed", timestamp)

    def finish_run(
        self, status: Literal["completed", "failed", "cancelled"], timestamp: str
    ) -> None:
        mapped: TrajectoryStatus = status
        self._finish_open_spans(mapped, timestamp)
        if not any(
            entry.category == "run" and entry.completed_at == timestamp for entry in self._entries
        ):
            self._instant("run", f"Run {status}", timestamp, status=mapped)
        timing = self._snapshot.timing
        self._snapshot = self._snapshot.model_copy(
            update={"timing": timing.model_copy(update={"completed_at": timestamp})}
        )

    def checkpoint_snapshot(self) -> RunObservabilitySnapshot:
        now = self._monotonic()
        elapsed = _elapsed_ms(self._last_tick, now)
        self._last_tick = now
        timing = self._snapshot.timing
        run_duration = (timing.run_duration_ms or 0) + elapsed
        self._snapshot = self._snapshot.model_copy(
            update={
                "timing": timing.model_copy(
                    update={"run_duration_ms": run_duration, "source": "derived"}
                ),
                "trajectory": tuple(self._entries),
            }
        )
        return self._snapshot

    def _start_phase(self, phase: str, title: str, timestamp: str) -> None:
        if phase in self._phase_spans:
            return
        sequence = self._next_sequence()
        entry = TrajectoryEntry(
            entry_id=_entry_id(self.run_id, sequence),
            sequence=sequence,
            category="run",
            status="started",
            title=title,
            started_at=timestamp,
            result_code=phase,
        )
        self._append(entry)
        self._phase_spans[phase] = (entry.entry_id, self._monotonic())

    def _finish_phase(self, phase: str, timestamp: str) -> None:
        active = self._phase_spans.pop(phase, None)
        if active is None:
            return
        duration = _elapsed_ms(active[1], self._monotonic()) if active[1] is not None else None
        self._replace_entry(
            active[0], status="completed", completed_at=timestamp, duration_ms=duration
        )
        if phase == "preparing_context" and duration is not None:
            current = self._snapshot.orchestration.context_build_duration_ms
            self._set_orchestration(
                context_build_duration_ms=(current or 0) + duration,
                source="derived",
            )

    def _start(
        self,
        category: TrajectoryCategory,
        title: str,
        timestamp: str,
        *,
        call_id: str | None = None,
        tool_name: str | None = None,
        status: Literal["started", "waiting"] = "started",
    ) -> None:
        key = self._key(category, call_id)
        if key in self._active:
            return
        sequence = self._next_sequence()
        entry = TrajectoryEntry(
            entry_id=_entry_id(self.run_id, sequence),
            sequence=sequence,
            category=category,
            status=status,
            title=title[:160],
            started_at=timestamp,
            call_id=call_id,
            tool_name=tool_name,
        )
        self._append(entry)
        self._active[key] = (entry.entry_id, self._monotonic())

    def _instant(
        self,
        category: TrajectoryCategory,
        title: str,
        timestamp: str,
        *,
        call_id: str | None = None,
        result_code: str | None = None,
        status: TrajectoryStatus = "completed",
        duration_ms: int | None = 0,
    ) -> None:
        sequence = self._next_sequence()
        self._append(
            TrajectoryEntry(
                entry_id=_entry_id(self.run_id, sequence),
                sequence=sequence,
                category=category,
                status=status,
                title=title[:160],
                started_at=timestamp,
                completed_at=timestamp,
                duration_ms=duration_ms,
                call_id=call_id,
                result_code=result_code,
            )
        )

    def _finish_category(
        self, category: TrajectoryCategory, status: TrajectoryStatus, timestamp: str
    ) -> int | None:
        return self._finish(self._key(category, None), status, timestamp)

    def _finish(
        self,
        key: str,
        status: TrajectoryStatus,
        timestamp: str,
        *,
        result_code: str | None = None,
    ) -> int | None:
        active = self._active.pop(key, None)
        if active is None:
            return None
        duration = _elapsed_ms(active[1], self._monotonic()) if active[1] is not None else None
        self._replace_entry(
            active[0],
            status=status,
            completed_at=timestamp,
            duration_ms=duration,
            result_code=result_code,
        )
        return duration

    def _finish_open_spans(self, status: TrajectoryStatus, timestamp: str) -> None:
        self.finish_model(timestamp, status)
        self.finish_interactions(timestamp, status)
        for key in [item for item in self._active if item.startswith("tool:")]:
            duration = self._finish(key, status, timestamp)
            if duration is not None:
                timing = self._snapshot.timing
                self._snapshot = self._snapshot.model_copy(
                    update={
                        "timing": timing.model_copy(
                            update={
                                "tool_duration_ms": _add_duration(timing.tool_duration_ms, duration)
                            }
                        )
                    }
                )
        for key in list(self._active):
            self._finish(key, status, timestamp)
        for phase, (entry_id, started) in list(self._phase_spans.items()):
            self._replace_entry(
                entry_id,
                status=status,
                completed_at=timestamp,
                duration_ms=(
                    _elapsed_ms(started, self._monotonic()) if started is not None else None
                ),
            )
            self._phase_spans.pop(phase, None)

    def _append(self, entry: TrajectoryEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) <= MAX_TRAJECTORY_ENTRIES:
            return
        self._snapshot = self._snapshot.model_copy(update={"truncated": True})
        first = self._entries[0]
        self._entries = [first, *self._entries[-(MAX_TRAJECTORY_ENTRIES - 1) :]]

    def _replace_entry(self, entry_id: str, **changes: object) -> None:
        for index, entry in enumerate(self._entries):
            if entry.entry_id == entry_id:
                self._entries[index] = entry.model_copy(update=changes)
                return

    def _next_sequence(self) -> int:
        return (self._entries[-1].sequence if self._entries else 0) + 1

    @staticmethod
    def _key(category: TrajectoryCategory, call_id: str | None) -> str:
        return f"{category}:{call_id or ''}"

    def _set_timing(self, **changes: object) -> None:
        self._snapshot = self._snapshot.model_copy(
            update={"timing": self._snapshot.timing.model_copy(update=changes)}
        )

    def _set_orchestration(self, **changes: object) -> None:
        value = self._snapshot.orchestration.model_copy(update=changes)
        self._snapshot = self._snapshot.model_copy(update={"orchestration": value})


def _entry_id(run_id: str, sequence: int) -> str:
    digest = hashlib.sha256(f"{run_id}:{sequence}".encode()).hexdigest()[:24]
    return f"traj_{digest}"


def _elapsed_ms(start: float, end: float) -> int:
    return max(0, int(round((end - start) * 1000)))


def _nonnegative(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sum_optional(base: int | None, current: int | None) -> int | None:
    if current is None:
        return None
    return (base or 0) + current


def _add_duration(total: int | None, value: int | None) -> int | None:
    if value is None:
        return total
    return (total or 0) + value


def _percent(value: int | None, limit: int | None) -> float | None:
    if value is None or limit is None or limit <= 0:
        return None
    return min(100.0, max(0.0, value / limit * 100))


__all__ = ["RunObservabilityRecorder", "new_observability"]
