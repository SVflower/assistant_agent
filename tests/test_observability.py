"""M34 Run 可观测记录器的确定性测试。"""

from __future__ import annotations

from assistant_agent.agent.run.observability import RunObservabilityRecorder, new_observability


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_model_usage_replaces_same_step_and_derives_timing() -> None:
    clock = _Clock()
    recorder = RunObservabilityRecorder(
        "run-observe", new_observability("run-observe", "2026-08-17T00:00:00Z"), monotonic=clock
    )
    recorder.update_estimated_context({"used": 120, "total": 1000})
    recorder.start_model("2026-08-17T00:00:01Z")
    clock.advance(0.1)
    assert recorder.first_model_signal("2026-08-17T00:00:01.100Z") is True
    recorder.observe_usage({"prompt_tokens": 200, "completion_tokens": 10})
    recorder.observe_usage({"prompt_tokens": 220, "completion_tokens": 20})
    clock.advance(0.9)
    recorder.finish_model("2026-08-17T00:00:02Z")
    snapshot = recorder.checkpoint_snapshot()

    assert snapshot.model_usage.input_tokens == 220
    assert snapshot.model_usage.output_tokens == 20
    assert snapshot.model_usage.cache_read_tokens is None
    assert snapshot.model_usage.cache_write_tokens is None
    assert snapshot.model_usage.cache_source == "unavailable"
    assert snapshot.context.used_tokens == 220
    assert snapshot.context.projected_tokens == 120
    assert snapshot.context.source == "provider"
    assert snapshot.timing.first_token_latency_ms == 100
    assert snapshot.timing.model_duration_ms == 1000
    assert snapshot.timing.tokens_per_second == 20 / 0.9


def test_cache_usage_is_only_present_when_provider_reports_it() -> None:
    recorder = RunObservabilityRecorder(
        "run-cache", new_observability("run-cache", "2026-08-17T00:00:00Z")
    )
    recorder.start_model("2026-08-17T00:00:01Z")
    recorder.observe_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "cache_read_tokens": 25,
            "cache_write_tokens": 2,
        }
    )
    recorder.finish_model("2026-08-17T00:00:02Z")
    usage = recorder.checkpoint_snapshot().model_usage

    assert usage.cache_read_tokens == 25
    assert usage.cache_write_tokens == 2
    assert usage.cache_hit_percent == 25.0
    assert usage.cache_source == "provider"


def test_tool_trajectory_uses_same_id_and_contains_no_arguments_or_output() -> None:
    clock = _Clock()
    recorder = RunObservabilityRecorder(
        "run-tool", new_observability("run-tool", "2026-08-17T00:00:00Z"), monotonic=clock
    )
    recorder.start_tool("call-1", "read_file", "2026-08-17T00:00:01Z")
    started = recorder.latest_entry
    clock.advance(0.25)
    recorder.finish_tool(
        "call-1",
        "2026-08-17T00:00:01.250Z",
        failed=False,
        result_code="ok",
    )
    completed = recorder.latest_entry

    assert started is not None and completed is not None
    assert started.entry_id == completed.entry_id
    assert completed.status == "completed"
    assert completed.duration_ms == 250
    assert completed.tool_name == "read_file"
    payload = completed.model_dump_json()
    assert "secret-path" not in payload
    assert "file-content" not in payload


def test_resume_closes_unmeasurable_span_without_duplicate_entry() -> None:
    first = RunObservabilityRecorder(
        "run-resume", new_observability("run-resume", "2026-08-17T00:00:00Z")
    )
    persisted = first.checkpoint_snapshot()
    recovered = RunObservabilityRecorder("run-resume", persisted)
    recovered.resume("2026-08-17T01:00:00Z")
    snapshot = recovered.checkpoint_snapshot()

    assert [entry.sequence for entry in snapshot.trajectory] == [1, 2]
    assert snapshot.trajectory[0].status == "paused"
    assert snapshot.trajectory[0].duration_ms is None
    assert snapshot.trajectory[1].status == "started"
    assert snapshot.timing.run_duration_ms is not None
    assert snapshot.timing.run_duration_ms < 60_000


def test_trajectory_is_bounded_and_marks_truncation() -> None:
    recorder = RunObservabilityRecorder(
        "run-long", new_observability("run-long", "2026-08-17T00:00:00Z")
    )
    for index in range(300):
        recorder.record_phase("saving_checkpoint", f"2026-08-17T00:00:{index:02d}Z")
    snapshot = recorder.checkpoint_snapshot()

    assert len(snapshot.trajectory) == 256
    assert snapshot.truncated is True
    assert snapshot.trajectory[0].sequence == 1
    assert snapshot.trajectory[-1].sequence == 301
