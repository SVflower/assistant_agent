"""M31 current-only 服务、checkpoint、Session 与 Chart 契约。"""

from __future__ import annotations

import json

import pytest

import assistant_agent.contracts as contracts
import assistant_agent.service as service
from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.contracts.charts import parse_chart_artifact
from assistant_agent.contracts.errors import (
    UnsupportedChartSchemaError,
    UnsupportedRunStateSchemaError,
    UnsupportedSessionSchemaError,
)
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.tools.charts import PresentChartTool
from tests.support import ToolContextFixture


def _chart_draft() -> dict:
    return {
        "schema_version": 2,
        "chart_type": "line",
        "title": "Current",
        "columns": [{"key": "x", "label": "X"}, {"key": "y", "label": "Y"}],
        "rows": [["A", 1]],
        "x_key": "x",
        "series": [{"key": "y", "label": "Y"}],
    }


def test_breaking_service_contract_version_and_public_exports():
    assert contracts.AGENT_SERVICE_CONTRACT_VERSION == 5
    assert service.AGENT_SERVICE_CONTRACT_VERSION == 5
    assert contracts.OUTPUT_CONTRACT_VERSION == 1
    assert not hasattr(contracts, "ChartSpecV1")
    assert not hasattr(service, "ChartArtifact")
    assert service.UnsupportedRunStateSchemaError.code == "unsupported_run_state_schema"
    assert service.UnsupportedSessionSchemaError.code == "unsupported_session_schema"
    assert service.UnsupportedChartSchemaError.code == "unsupported_chart_schema"


def test_run_store_rejects_all_legacy_versions_without_writing(tmp_path):
    store = RunStore(tmp_path / "runs")
    for version in range(1, 10):
        with pytest.raises(UnsupportedRunStateSchemaError) as caught:
            store.save("run-old", {"schema_version": version, "run_id": "run-old"})
        assert caught.value.expected_version == 10
        assert caught.value.actual_version == version
    assert not (tmp_path / "runs" / "run-old.json").exists()


def test_coordinator_load_rejects_incompatible_checkpoint_without_fallback(tmp_path):
    store = RunStore(tmp_path / "runs")
    path = store._path("run-old")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version":6,"run_id":"run-old"}', encoding="utf-8")
    with pytest.raises(UnsupportedRunStateSchemaError):
        RunCoordinator.load(store, "run-old")


@pytest.mark.parametrize("version", [None, 0, 1, 2, 3, 4])
def test_session_store_rejects_non_v5_without_rewriting(tmp_path, version):
    store = SessionStore(tmp_path / "sessions")
    path = store._path("old")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": version, "id": "old"}), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(UnsupportedSessionSchemaError) as caught:
        store.load("old")
    assert caught.value.expected_version == 5
    assert caught.value.actual_version == version
    assert path.read_bytes() == original


def test_chart_parser_and_tool_only_accept_v2(tmp_path):
    with pytest.raises(UnsupportedChartSchemaError) as caught:
        parse_chart_artifact({"schema_version": 1})
    assert caught.value.expected_version == 2
    assert caught.value.actual_version == 1

    tool = PresentChartTool()
    assert tool.parameters["properties"]["schema_version"] == {"const": 2}
    context = ToolContextFixture(
        workspace_root=tmp_path,
        current_call_id="call",
        current_run_id="run",
        current_session_id="session",
    )
    result = tool.run(_chart_draft(), context)
    assert result.chart is not None and result.chart.schema_version == 2
    rejected = tool.run({**_chart_draft(), "schema_version": 1}, context)
    assert rejected.code == "artifact_rejected"
    assert rejected.chart is None
