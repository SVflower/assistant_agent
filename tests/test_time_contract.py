"""M23-R1 UTC 时间冻结规则。"""

from __future__ import annotations

from assistant_agent.agent.run.state import RunState, now_iso
from assistant_agent.contracts.time import normalize_utc_timestamp, parse_utc_timestamp


def test_mixed_legacy_and_rfc3339_times_normalize_deterministically(monkeypatch):
    expected = "2026-01-01T00:00:00Z"
    values = (
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00Z",
        "2026-01-01T08:00:00+08:00",
        "2025-12-31T19:00:00-05:00",
    )
    for timezone in ("UTC", "Asia/Shanghai", "America/New_York"):
        monkeypatch.setenv("TZ", timezone)
        assert {normalize_utc_timestamp(value) for value in values} == {expected}
        assert len({parse_utc_timestamp(value) for value in values}) == 1


def test_new_run_timestamps_are_utc_rfc3339_z():
    assert now_iso().endswith("Z")
    assert RunState.model_fields["schema_version"].default == 7


def test_fractional_seconds_preserve_distinct_instants_and_normalize_offsets():
    assert normalize_utc_timestamp("2026-01-01T00:00:00.1") == ("2026-01-01T00:00:00.100000Z")
    assert normalize_utc_timestamp("2026-01-01T01:00:00.9+01:00") == ("2026-01-01T00:00:00.900000Z")
    assert parse_utc_timestamp("2026-01-01T00:00:00.1Z") < parse_utc_timestamp(
        "2026-01-01T00:00:00.9Z"
    )
