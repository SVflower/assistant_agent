"""M24 Chart Artifact 冻结公共契约。"""

from __future__ import annotations

import hashlib
from dataclasses import fields

import pytest
from pydantic import ValidationError

from assistant_agent.contracts import (
    EVENT_CONTRACT_VERSION,
    ChartSpecV1,
    StepEvent,
)
from assistant_agent.contracts.charts import build_chart_artifact, canonical_json_bytes
from assistant_agent.tools.models import ToolResult


def _spec(**updates):
    value = {
        "schema_version": 1,
        "chart_type": "line",
        "title": "月度趋势",
        "description": None,
        "source_label": "用户提供的数据",
        "columns": [
            {"key": "month", "label": "月份", "data_type": "string", "unit": None},
            {"key": "value", "label": "数量", "data_type": "number", "unit": "件"},
        ],
        "rows": [["1月", 12], ["2月", 18]],
        "x_key": "month",
        "series": [{"key": "value", "label": "数量"}],
        "category_key": None,
        "value_key": None,
    }
    value.update(updates)
    return ChartSpecV1.model_validate(value)


def test_event_contract_is_additive_and_version_stays_v1():
    artifact = build_chart_artifact(
        _spec(), session_id="session-1", run_id="run-1", call_id="call-1"
    )
    result = ToolResult.ok("ok", chart=artifact)
    event = StepEvent(kind="tool_result", chart=result.chart)
    assert EVENT_CONTRACT_VERSION == 1
    assert event.chart == artifact
    assert ToolResult(output="failed", is_error=True, chart=artifact).chart is None


def test_public_chart_and_event_contract_fields_are_unchanged():
    assert set(ChartSpecV1.model_fields) == {
        "schema_version",
        "chart_type",
        "title",
        "description",
        "source_label",
        "columns",
        "rows",
        "x_key",
        "series",
        "category_key",
        "value_key",
    }
    assert [field.name for field in fields(StepEvent)][-1] == "chart"


def test_content_hash_uses_frozen_canonical_json():
    spec = _spec()
    artifact = build_chart_artifact(spec, session_id="session-1", run_id="run-1", call_id="call-1")
    expected = hashlib.sha256(canonical_json_bytes(spec.model_dump(mode="json"))).hexdigest()
    assert artifact.content_hash == f"sha256:{expected}"
    assert artifact.ref.model_dump().keys() == {
        "artifact_id",
        "kind",
        "schema_version",
        "content_hash",
        "session_id",
        "run_id",
        "message_id",
        "created_at",
        "title",
        "size_bytes",
    }
    damaged = artifact.model_dump(mode="json")
    damaged["spec"]["rows"][0][1] = 999
    with pytest.raises(ValidationError, match="content_hash"):
        type(artifact).model_validate(damaged)


@pytest.mark.parametrize("forbidden", ["option", "formatter", "html", "url", "graphic"])
def test_arbitrary_rendering_configuration_is_forbidden(forbidden):
    with pytest.raises(ValidationError):
        _spec(**{forbidden: {}})


def test_encoding_and_data_limits_are_strict():
    columns = [
        {"key": f"c{index}", "label": f"C{index}", "data_type": "number"} for index in range(5)
    ]
    with pytest.raises(ValidationError, match="20000"):
        _spec(
            columns=columns,
            rows=[[index] * 5 for index in range(4001)],
            x_key="c0",
            series=[{"key": "c1", "label": "C1"}],
        )
    with pytest.raises(ValidationError, match="valid integer|valid number"):
        _spec(rows=[["x", True]])
    with pytest.raises(ValidationError, match="finite|有限"):
        _spec(rows=[["x", float("inf")]])
    with pytest.raises(ValidationError, match="scatter"):
        _spec(chart_type="scatter")
    with pytest.raises(ValidationError, match="number 列"):
        _spec(rows=[["x", "not-a-number"]])


def test_artifact_size_limit_applies_after_valid_spec():
    columns = [
        {"key": "x", "label": "X", "data_type": "string"},
        *[{"key": f"v{index}", "label": f"V{index}", "data_type": "string"} for index in range(3)],
    ]
    spec = _spec(
        columns=columns,
        rows=[[f"{row}-" + "x" * 30] * 4 for row in range(5000)],
        x_key="x",
        series=[{"key": "v0", "label": "V0"}],
    )
    with pytest.raises(ValueError, match="512 KiB"):
        build_chart_artifact(spec, session_id="s", run_id="r", call_id="c")
