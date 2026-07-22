"""M28 ChartSpecV2 的确定性、安全与兼容契约。"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from assistant_agent.agent.run.state import RunState, migrate_run_document
from assistant_agent.application.models import Session
from assistant_agent.contracts import EVENT_CONTRACT_VERSION, ChartArtifactV2, StepEvent
from assistant_agent.contracts.charts import (
    ChartSpecV1,
    build_chart_artifact,
    canonical_json_bytes,
    parse_chart_artifact,
    stable_message_id,
)
from assistant_agent.contracts.charts_v2 import build_chart_artifact_v2
from assistant_agent.contracts.sessions import PublicMessageSnapshot
from assistant_agent.persistence.store import SessionStore
from assistant_agent.tools.chart_input import ChartInputError
from assistant_agent.tools.chart_input_v2 import normalize_chart_v2_input
from assistant_agent.tools.charts import PresentChartTool
from tests.support import ToolContextFixture


def _draft(chart_type: str, **updates):
    value = {
        "schema_version": 2,
        "chart_type": chart_type,
        "title": f"{chart_type} chart",
        "columns": [
            {"key": "x", "label": "X", "data_type": "string"},
            {"key": "group", "label": "Group", "data_type": "string"},
            {"key": "nx", "label": "Numeric X", "data_type": "number"},
            {"key": "a", "label": "A", "data_type": "number"},
            {"key": "b", "label": "B", "data_type": "number"},
            {"key": "size", "label": "Size", "data_type": "number"},
            {"key": "low", "label": "Low", "data_type": "number"},
            {"key": "high", "label": "High", "data_type": "number"},
        ],
        "rows": [
            ["A", "G1", 1, 2, 8, 3, 1, 3],
            ["B", "G1", 2, 4, 6, 4, 3, 5],
            ["C", "G2", 3, 6, 4, 5, 5, 7],
            ["D", "G2", 4, 8, 2, 6, 7, 9],
        ],
        "x_key": "x",
        "series": [{"key": "a", "label": "A"}],
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    ("chart_type", "updates"),
    [
        ("line", {}),
        ("area", {}),
        ("bar", {}),
        ("grouped_bar", {"series": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}),
        ("stacked_bar", {"series": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}),
        (
            "percent_stacked_bar",
            {"series": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]},
        ),
        ("pie", {"category_key": "x", "value_key": "a", "series": []}),
        ("donut", {"category_key": "x", "value_key": "a", "series": []}),
        ("combo_bar_line", {"series": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}),
        (
            "dual_axis",
            {
                "series": [
                    {"key": "a", "label": "A", "axis": "left"},
                    {"key": "b", "label": "B", "axis": "right"},
                ]
            },
        ),
        ("scatter", {"x_key": "nx"}),
        ("bubble", {"x_key": "nx", "size_key": "size"}),
        ("histogram", {"value_key": "a", "bin_count": 2, "series": []}),
        ("boxplot", {"value_key": "a", "group_key": "group", "series": []}),
        ("heatmap", {"x_key": "x", "y_key": "group", "value_key": "a", "series": []}),
    ],
)
def test_all_frozen_chart_types_normalize_to_v2(chart_type, updates):
    spec = normalize_chart_v2_input(_draft(chart_type, **updates))
    assert spec.schema_version == 2
    assert spec.panels[0].chart_type == chart_type
    assert len(spec.panels[0].series) >= 1


def test_heatmap_normalizes_canonical_axes_as_two_categories():
    spec = normalize_chart_v2_input(
        _draft("heatmap", x_key="x", y_key="group", value_key="a", series=[])
    )
    panel = spec.panels[0]
    assert panel.x_axis is not None
    assert panel.x_axis.scale == "category"
    assert panel.y_axes[0].scale == "category"
    canonical = spec.model_dump(mode="json")
    assert canonical["panels"][0]["x_axis"]["scale"] == "category"
    assert canonical["panels"][0]["y_axes"] == [
        {
            "axis_id": "axis_y",
            "dimension": "y",
            "scale": "category",
            "position": "left",
            "title": None,
        }
    ]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "rows"),
        ([[None, "G", 1, 2, 3, 1, 1, 3]], "x"),
        ([["   ", "G", 1, 2, 3, 1, 1, 3]], "x"),
        ([["A", None, 1, 2, 3, 1, 1, 3]], "group"),
        ([["A", "", 1, 2, 3, 1, 1, 3]], "group"),
        ([["A", "G", 1, None, 3, 1, 1, 3]], "value_key"),
    ],
)
def test_heatmap_rejects_empty_or_non_renderable_data(rows, message):
    with pytest.raises(ChartInputError, match=message):
        normalize_chart_v2_input(
            _draft(
                "heatmap",
                x_key="x",
                y_key="group",
                value_key="a",
                series=[],
                rows=rows,
            )
        )


def test_local_model_heatmap_draft_can_omit_column_types():
    draft = _draft("heatmap", x_key="x", y_key="group", value_key="a", series=[])
    for column in draft["columns"]:
        column.pop("data_type")
    spec = normalize_chart_v2_input(draft)
    assert spec.panels[0].chart_type == "heatmap"
    assert spec.datasets[1].rows


def test_histogram_and_boxplot_use_frozen_deterministic_algorithms():
    histogram = normalize_chart_v2_input(_draft("histogram", value_key="a", bin_count=2, series=[]))
    histogram_rows = histogram.datasets[1].rows
    assert [row[3] for row in histogram_rows] == [2, 2]
    assert histogram.derivations[0].algorithm == "explicit_bins_v1"

    box = normalize_chart_v2_input(
        _draft(
            "boxplot",
            value_key="a",
            group_key=None,
            series=[],
            rows=[
                [str(index), "G", index, value, 1, 1, value, value]
                for index, value in enumerate([1, 2, 3, 4, 100], start=1)
            ],
        )
    )
    assert box.datasets[1].rows[0][1:6] == (1.0, 2.0, 3.0, 4.0, 4.0)
    assert box.datasets[2].rows == (("全部", 100.0),)
    assert box.panels[0].series[0].outlier_dataset_id == box.datasets[2].dataset_id
    assert box.derivations[0].algorithm == "type7_iqr_v1"


def test_histogram_zero_iqr_uses_sturges_fallback():
    spec = normalize_chart_v2_input(
        _draft(
            "histogram",
            value_key="a",
            series=[],
            rows=[[str(i), "G", i, 5, 1, 1, 4, 6] for i in range(4)],
        )
    )
    assert spec.derivations[0].algorithm == "sturges_v1"
    assert len(spec.datasets[1].rows) == 1


def test_multi_panel_overlays_error_bars_and_dual_axis_are_controlled():
    draft = _draft(
        "line",
        panels=[
            {
                "chart_type": "line",
                "panel_title": "Trend",
                "x_key": "x",
                "series": [{"key": "a", "label": "A"}],
                "reference_lines": [{"axis": "left", "value": 5, "label": "Target"}],
                "reference_bands": [{"axis": "left", "start": 2, "end": 8}],
                "error_bars": [{"series_key": "a", "lower_key": "low", "upper_key": "high"}],
                "annotations": [{"text": "Peak", "x_value": "D", "y_value": 8}],
            },
            {
                "chart_type": "dual_axis",
                "x_key": "x",
                "series": [
                    {"key": "a", "label": "A", "axis": "left"},
                    {"key": "b", "label": "B", "axis": "right", "mark": "bar"},
                ],
            },
        ],
        layout={"columns": 2, "shared_legend": False},
    )
    spec = normalize_chart_v2_input(draft)
    assert spec.layout.columns == 2
    assert len(spec.panels) == 2
    assert len(spec.panels[1].y_axes) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda draft: draft.update(option={}),
        lambda draft: draft["series"][0].update(formatter="function(){return 1}"),
        lambda draft: draft.update(style={"__proto__": {}}),
        lambda draft: draft.update(url="https://example.com"),
        lambda draft: draft.update(graphic=[]),
    ],
)
def test_v2_draft_recursively_rejects_executable_or_renderer_fields(mutation):
    draft = _draft("line")
    mutation(draft)
    with pytest.raises(ChartInputError, match="禁止"):
        normalize_chart_v2_input(draft)


def test_v2_draft_rejects_unknown_fields_instead_of_ignoring_them():
    draft = _draft("line")
    draft["series"][0]["color"] = "red"
    with pytest.raises(ChartInputError, match="未支持字段"):
        normalize_chart_v2_input(draft)


@pytest.mark.parametrize(
    "draft",
    [
        _draft("bubble", x_key="nx", size_key="size", rows=[["A", "G", 1, 2, 3, -1, 1, 3]]),
        _draft(
            "percent_stacked_bar",
            series=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
            rows=[["A", "G", 1, -1, 1, 1, -2, 2]],
        ),
        _draft(
            "pie",
            category_key="x",
            value_key="a",
            series=[],
            rows=[["A", "G", 1, 2, 3, 1, 1, 3], ["A", "G", 2, 4, 3, 1, 3, 5]],
        ),
        _draft(
            "heatmap",
            x_key="x",
            y_key="group",
            value_key="a",
            series=[],
            rows=[["A", "G", 1, 2, 3, 1, 1, 3], ["A", "G", 2, 4, 3, 1, 3, 5]],
        ),
    ],
)
def test_v2_ambiguous_or_unsafe_values_fail_closed(draft):
    with pytest.raises(ChartInputError):
        normalize_chart_v2_input(draft)


def test_v1_canonical_bytes_and_hash_remain_frozen():
    spec = ChartSpecV1.model_validate(
        {
            "schema_version": 1,
            "chart_type": "line",
            "title": "Frozen",
            "columns": [
                {"key": "x", "label": "X", "data_type": "string"},
                {"key": "y", "label": "Y", "data_type": "number"},
            ],
            "rows": [["A", 1]],
            "x_key": "x",
            "series": [{"key": "y", "label": "Y"}],
        }
    )
    payload = canonical_json_bytes(spec.model_dump(mode="json"))
    assert (
        hashlib.sha256(payload).hexdigest()
        == "72da6f43ed0e1adef450f6ecfa6a881f75fdb8e667a0c5058cbd993c85d32e35"
    )
    artifact = build_chart_artifact(
        spec,
        session_id="session-1",
        run_id="run-1",
        call_id="call-1",
        created_at="2026-01-01T00:00:00Z",
    )
    assert parse_chart_artifact(json.loads(artifact.model_dump_json()), strict=True) == artifact


def test_v2_artifact_is_additive_on_event_v1_and_tool_uses_one_retry_policy(tmp_path):
    spec = normalize_chart_v2_input(_draft("line"))
    artifact = build_chart_artifact_v2(
        spec, session_id="session-1", run_id="run-1", call_id="call-1"
    )
    assert isinstance(parse_chart_artifact(artifact.model_dump(mode="json")), ChartArtifactV2)
    event = StepEvent(kind="tool_result", chart=artifact)
    assert EVENT_CONTRACT_VERSION == 1
    assert event.chart == artifact

    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="call-1",
        current_run_id="run-1",
        current_session_id="session-1",
    )
    result = PresentChartTool().run(_draft("line"), ctx)
    assert result.chart is not None and result.chart.schema_version == 2


def test_run_checkpoint_versions_1_through_6_migrate_to_v7():
    current = {
        "schema_version": 7,
        "run_id": "run-1",
        "session_id": None,
        "task": "chart",
        "status": "running",
        "phase": "model_pending",
        "interactive": False,
        "provider": "p",
        "model": "m",
        "system_prompt_hash": "a" * 64,
        "tool_schema_hash": "b" * 64,
        "messages": [],
        "iteration": 0,
        "iteration_budget": 5,
        "tool_budget": {
            "max_calls": 5,
            "max_total_output_chars": 100,
            "used_calls": 0,
            "used_output_chars": 0,
        },
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    for version in range(1, 7):
        old = copy.deepcopy(current)
        old["schema_version"] = version
        migrated = migrate_run_document(old)
        assert migrated["schema_version"] == 7
        assert RunState.model_validate(migrated).presentations == []


def test_session_v2_migrates_to_v3_and_fork_deep_copies_v2_artifact(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    run_id = "run-chart-v2"
    assistant_id = stable_message_id(run_id)
    artifact = build_chart_artifact_v2(
        normalize_chart_v2_input(_draft("line")),
        session_id="source",
        run_id=run_id,
        call_id="call-1",
        created_at="2026-01-01T00:00:00Z",
    )
    source = Session(
        id="source",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        title="Source",
        provider="p",
        model="m",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "chart"},
            {"role": "user", "content": "second"},
        ],
        presentations=[artifact],
        message_ledger=[
            PublicMessageSnapshot(id="msg_111111111111111111111111", role="user", content="first"),
            PublicMessageSnapshot(
                id=assistant_id,
                role="assistant",
                reply_to_message_id="msg_111111111111111111111111",
                content="chart",
                artifacts=(artifact.ref,),
            ),
            PublicMessageSnapshot(id="msg_222222222222222222222222", role="user", content="second"),
        ],
    )
    store.save(source, must_exist=False)
    path = store._path("source")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 2
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    migrated = store.load("source")
    assert migrated.schema_version == 3
    assert isinstance(migrated.presentations[0], ChartArtifactV2)
    forked, created = store.fork_session(
        "source", "msg_222222222222222222222222", "a" * 64, "b" * 64
    )
    assert created
    assert forked.schema_version == 3
    assert len(forked.presentations) == 1
    cloned = forked.presentations[0]
    assert isinstance(cloned, ChartArtifactV2)
    assert cloned.artifact_id != artifact.artifact_id
    assert cloned.content_hash == artifact.content_hash
    assert cloned.session_id == forked.id
    assert cloned.run_id is None
    assert store.load("source").presentations[0] == artifact


def test_tool_schema_remains_compact_and_avoids_canonical_union():
    schema = PresentChartTool().parameters
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    assert len(encoded.encode("utf-8")) < 12_000
    assert "datasets" not in encoded
    assert "derivations" not in encoded
    assert "oneOf" not in encoded


def test_v2_cells_series_and_artifact_size_hard_limits():
    oversized_cells = _draft(
        "line",
        rows=[[str(index), "G", index, 1, 2, 3, 0, 2] for index in range(2501)],
    )
    with pytest.raises(ChartInputError, match="20000"):
        normalize_chart_v2_input(oversized_cells)

    too_many_series = _draft(
        "line",
        panels=[
            {
                "chart_type": "grouped_bar",
                "x_key": "x",
                "series": [
                    {"key": "a", "label": "A"},
                    {"key": "b", "label": "B"},
                    {"key": "size", "label": "Size"},
                ],
            }
            for _ in range(3)
        ],
    )
    with pytest.raises(ChartInputError, match="series"):
        normalize_chart_v2_input(too_many_series)

    large = _draft(
        "line",
        columns=[
            {"key": "x", "label": "X", "data_type": "string"},
            {"key": "a", "label": "A", "data_type": "number"},
        ],
        rows=[["x" * 120, index] for index in range(5000)],
        x_key="x",
    )
    spec = normalize_chart_v2_input(large)
    with pytest.raises(ValueError, match="512 KiB"):
        build_chart_artifact_v2(spec, session_id="s", run_id="r", call_id="c")
