"""present_chart 工具、checkpoint 和 Session 历史测试。"""

from __future__ import annotations

import json

import pytest

from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.providers.ports import ToolCall
from assistant_agent.tools.charts import PresentChartTool
from assistant_agent.tools.registry import ToolRegistry
from tests.support import ToolContextFixture


def _args(title="趋势"):
    return {
        "schema_version": 2,
        "chart_type": "line",
        "title": title,
        "columns": [
            {"key": "x", "label": "X", "data_type": "string", "unit": None},
            {"key": "y", "label": "Y", "data_type": "number", "unit": None},
        ],
        "rows": [["a", 1], ["b", 2]],
        "x_key": "x",
        "series": [{"key": "y", "label": "Y"}],
        "category_key": None,
        "value_key": None,
    }


def _draft(rows=None):
    args = _args()
    for column in args["columns"]:
        column.pop("data_type")
    if rows is not None:
        args["rows"] = rows
    return args


def _coordinator(tmp_path):
    return RunCoordinator.create(
        RunStore(tmp_path / "runs"),
        task="chart",
        provider="p",
        model="m",
        system_prompt="sys",
        tool_schemas=[],
        interactive=True,
        max_iterations=5,
        max_tool_calls=20,
        max_total_tool_output_chars=10_000,
        session_id="session-1",
        run_id="run-1",
    )


def _plan(coordinator, call_id, arguments=None):
    call_arguments = arguments or _args(call_id)
    call = ToolCall(call_id, "present_chart", call_arguments)
    messages = list(coordinator.state.messages)
    messages.extend(
        [
            {"role": "user", "content": "chart"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "present_chart",
                            "arguments": json.dumps(call_arguments),
                        },
                    }
                ],
            },
        ]
    )
    coordinator.model_completed(
        messages,
        [call],
    )


def test_present_chart_is_pure_and_safely_idempotent(tmp_path):
    registry = ToolRegistry()
    registry.register(PresentChartTool())
    coordinator = _coordinator(tmp_path)
    _plan(coordinator, "call-1")
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_run_id="run-1",
        current_session_id="session-1",
    )
    result = registry.execute(
        "present_chart", _args(), ctx, call_id="call-1", lifecycle=coordinator
    )
    assert not result.is_error
    assert result.chart is not None
    assert coordinator.state.tool_calls[0].replay_policy == "safe_idempotent"
    loaded = RunCoordinator.load(RunStore(tmp_path / "runs"), "run-1")
    assert loaded.state.presentations == [result.chart]
    assert loaded.result_for("call-1").chart == result.chart


def test_missing_column_types_are_inferred_for_local_model_style(tmp_path):
    tool = PresentChartTool()
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="c",
        current_run_id="r",
        current_session_id="s",
    )
    result = tool.run(_draft(), ctx)
    assert not result.is_error
    assert [column.data_type for column in result.chart.spec.datasets[0].columns] == [
        "string",
        "number",
    ]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1, 2.5], "number"),
        (["A", "B"], "string"),
        (["2026-07-20", "2026-07-21T10:30:00Z"], "datetime"),
    ],
)
def test_missing_type_inference_is_deterministic(tmp_path, values, expected):
    args = _args()
    args["columns"] = [
        {"key": "x", "label": "X"},
        {"key": "y", "label": "Y"},
    ]
    args["rows"] = [[value, index + 1] for index, value in enumerate(values)]
    args["x_key"] = "x"
    args["series"] = [{"key": "y", "label": "Y"}]
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="c",
        current_run_id="r",
        current_session_id="s",
    )
    result = PresentChartTool().run(args, ctx)
    assert not result.is_error
    assert result.chart.spec.datasets[0].columns[0].data_type == expected


@pytest.mark.parametrize(
    "rows",
    [
        [["a", None], ["b", None]],
        [["a", 1], ["b", "2"]],
        [["a", True], ["b", False]],
        [["a", float("inf")], ["b", 2]],
        [["2026-07-20", 1], ["ordinary", 2]],
        [["a"], ["b", 2]],
    ],
)
def test_ambiguous_or_unsafe_drafts_fail_closed(tmp_path, rows):
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="c",
        current_run_id="r",
        current_session_id="s",
    )
    result = PresentChartTool().run(_draft(rows), ctx)
    assert result.code == "artifact_rejected"
    assert result.chart is None
    assert "[chart_input_invalid]" in result.output


@pytest.mark.parametrize(
    "forbidden",
    ["option", "formatter", "html", "url", "graphic", "script", "function", "__proto__"],
)
def test_registry_rejects_non_declarative_chart_fields(tmp_path, forbidden):
    registry = ToolRegistry()
    registry.register(PresentChartTool())
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_run_id="run-1",
        current_session_id="session-1",
    )
    result = registry.execute("present_chart", {**_draft(), forbidden: {}}, ctx, call_id="c")
    assert result.code == "artifact_rejected"
    assert result.chart is None
    assert forbidden not in result.output


def test_unknown_column_reference_is_rejected_with_safe_error(tmp_path):
    args = _draft()
    args["series"] = [{"key": "missing", "label": "未知"}]
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="c",
        current_run_id="r",
        current_session_id="s",
    )
    result = PresentChartTool().run(args, ctx)
    assert result.code == "artifact_rejected"
    assert "series 字段必须引用已声明列" in result.output
    assert "missing" not in result.output


def test_chart_correction_limit_survives_checkpoint_reload(tmp_path):
    coordinator = _coordinator(tmp_path)
    registry = ToolRegistry()
    registry.register(PresentChartTool())
    invalid_args = _draft([["a", None]])
    _plan(coordinator, "call-1", invalid_args)
    ctx = ToolContextFixture(workspace_root=tmp_path)
    ctx.bind_run(
        "run-1",
        "session-1",
        result_count=coordinator.count_tool_results,
    )
    first = registry.execute(
        "present_chart", invalid_args, ctx, call_id="call-1", lifecycle=coordinator
    )
    assert first.retryable

    loaded = RunCoordinator.load(RunStore(tmp_path / "runs"), "run-1")
    resumed_ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="call-2",
        current_run_id="run-1",
        current_session_id="session-1",
        result_count=loaded.count_tool_results,
        result_count_matching=loaded.count_tool_results_matching,
    )
    second = PresentChartTool().run(_draft([["a", None]]), resumed_ctx)
    assert not second.retryable
    assert "修正次数已用完" in second.output

    corrected = PresentChartTool().run(_draft(), resumed_ctx)
    assert not corrected.is_error


def _duplicate_heatmap(title: str, *, aggregate=None, panels=False):
    panel = {
        "chart_type": "heatmap",
        "x_key": "x",
        "y_key": "group",
        "value_key": "value",
        "aggregate": aggregate,
    }
    args = {
        "schema_version": 2,
        "chart_type": "heatmap",
        "title": title,
        "columns": [
            {"key": "x", "label": "X"},
            {"key": "group", "label": "Group"},
            {"key": "value", "label": "Value"},
        ],
        "rows": [["A", "G", 1], ["A", "G", 2]],
        "x_key": "x",
        "y_key": "group",
        "value_key": "value",
        "aggregate": aggregate,
        "series": [],
    }
    if panels:
        args["panels"] = [panel]
    return args


def test_multi_panel_aggregate_error_has_safe_structured_correction_metadata(tmp_path):
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="call-1",
        current_run_id="run-1",
        current_session_id="session-1",
    )
    result = PresentChartTool().run(_duplicate_heatmap("Heat", panels=True), ctx)
    assert result.is_error
    assert result.retryable
    assert result.metadata == {
        "field_path": "panels[0].aggregate",
        "allowed_values": ["count", "sum", "mean", "min", "max"],
        "duplicate_coordinate": ["A", "G"],
        "duplicate_count": 2,
        "correction_remaining": 1,
    }
    assert "panels[0].aggregate" in result.output
    assert "correction_remaining=1" in result.output


def test_chart_correction_quota_is_isolated_by_chart_intent(tmp_path):
    coordinator = _coordinator(tmp_path)
    registry = ToolRegistry()
    registry.register(PresentChartTool())
    ctx = ToolContextFixture(workspace_root=tmp_path)
    ctx.bind_run(
        "run-1",
        "session-1",
        result_count=coordinator.count_tool_results,
        result_count_matching=coordinator.count_tool_results_matching,
    )

    first_args = _duplicate_heatmap("First")
    _plan(coordinator, "call-1", first_args)
    first = registry.execute(
        "present_chart", first_args, ctx, call_id="call-1", lifecycle=coordinator
    )
    assert first.retryable
    coordinator.batch_completed(coordinator.state.messages)

    second_args = _duplicate_heatmap("Second")
    _plan(coordinator, "call-2", second_args)
    second = registry.execute(
        "present_chart", second_args, ctx, call_id="call-2", lifecycle=coordinator
    )
    assert second.retryable
    coordinator.batch_completed(coordinator.state.messages)

    _plan(coordinator, "call-3", second_args)
    exhausted = registry.execute(
        "present_chart", second_args, ctx, call_id="call-3", lifecycle=coordinator
    )
    assert not exhausted.retryable
    assert exhausted.metadata["correction_remaining"] == 0


def test_tool_rejects_unbound_or_invalid_artifact(tmp_path):
    tool = PresentChartTool()
    assert tool.run(_args(), ToolContextFixture(workspace_root=tmp_path)).code == (
        "artifact_rejected"
    )
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="c",
        current_run_id="r",
        current_session_id="s",
    )
    assert tool.run({**_args(), "option": {}}, ctx).code == "artifact_rejected"


def test_run_rejects_seventeenth_artifact_without_losing_first_sixteen(tmp_path):
    coordinator = _coordinator(tmp_path)
    registry = ToolRegistry()
    registry.register(PresentChartTool())
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        current_run_id="run-1",
        current_session_id="session-1",
    )
    for index in range(17):
        call_id = f"call-{index}"
        _plan(coordinator, call_id)
        result = registry.execute(
            "present_chart", _args(str(index)), ctx, call_id=call_id, lifecycle=coordinator
        )
        coordinator.batch_completed(coordinator.state.messages)
    assert len(coordinator.state.presentations) == 16
    assert result.code == "artifact_rejected"
    assert result.chart is None


def test_service_exports_chart_contract():
    from assistant_agent import service

    assert service.ChartSpecV2 is not None
    assert service.ChartArtifactV2 is not None
    assert not hasattr(service, "ChartSpecV1")
    assert not hasattr(service, "ChartArtifact")
    assert issubclass(service.ArtifactNotFoundError, RuntimeError)
