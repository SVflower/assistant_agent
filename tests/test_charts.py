"""present_chart 工具、checkpoint 和 Session 历史测试。"""

from __future__ import annotations

import copy

from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.agent.run.state import RunState, migrate_run_document
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.providers.ports import ToolCall
from assistant_agent.tools.charts import PresentChartTool
from assistant_agent.tools.registry import ToolRegistry
from tests.support import ToolContextFixture


def _args(title="趋势"):
    return {
        "schema_version": 1,
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


def _plan(coordinator, call_id):
    call = ToolCall(call_id, "present_chart", _args(call_id))
    coordinator.model_completed(
        [
            {"role": "user", "content": "chart"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "present_chart", "arguments": "{}"},
                    }
                ],
            },
        ],
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


def test_checkpoint_migrates_v1_v2_v3_to_v4_without_presentations(tmp_path):
    coordinator = _coordinator(tmp_path)
    document = coordinator.state.model_dump(mode="json")
    for version in (1, 2, 3):
        old = copy.deepcopy(document)
        old["schema_version"] = version
        old.pop("presentations")
        migrated = migrate_run_document(old)
        state = RunState.model_validate(migrated)
        assert state.schema_version == 4
        assert state.presentations == []


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

    assert service.ChartSpecV1 is not None
    assert service.ChartArtifact is not None
    assert issubclass(service.ArtifactNotFoundError, RuntimeError)
