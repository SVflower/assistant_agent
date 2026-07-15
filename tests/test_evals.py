"""M9c 行为 eval 基础设施测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals import runner
from evals.cli import main
from evals.loader import EvalLoadError, confined_path, load_cases, safe_relative_path
from evals.report import build_metadata, compare_reports, read_report, write_report
from evals.runner import run_cases
from evals.schema import (
    CaseMetrics,
    CaseResult,
    EvalCase,
    RunMetadata,
    ScriptRound,
    TraceCall,
)
from evals.scorers import score_case
from evals.scripted_client import ScriptedClient


def _case(**overrides) -> EvalCase:
    data = {
        "id": "sample",
        "title": "sample",
        "task": "read",
        "script": [{"final": "done"}],
    }
    data.update(overrides)
    return EvalCase.model_validate(data)


def _result(case_id: str = "sample", *, repetition: int = 1, passed: bool = True) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        mode="scripted",
        repetition=repetition,
        passed=passed,
        outcome="success",
        metrics=CaseMetrics(tool_calls=1),
    )


def _metadata(**overrides) -> RunMetadata:
    data = {
        "mode": "scripted",
        "model_capability": False,
        "git_commit": "abc",
        "python": "3.11",
        "platform": "test",
        "provider": "scripted",
        "model": "scripted",
        "prompt_hash": "prompt",
        "tool_schema_hash": "tools",
        "permission_mode": "per-case",
        "skills_enabled": False,
        "mcp_enabled": False,
        "compaction_enabled": False,
        "started_at": "2026-01-01T00:00:00",
    }
    data.update(overrides)
    return RunMetadata.model_validate(data)


def test_schema_rejects_unknown_version_and_fields():
    with pytest.raises(ValidationError):
        _case(schema_version=2)
    with pytest.raises(ValidationError):
        _case(unknown=True)
    with pytest.raises(ValidationError):
        ScriptRound.model_validate({"final": "x", "error": "y"})


def test_loader_rejects_duplicate_ids_and_path_escape(tmp_path):
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        "- {id: same, title: one, task: x}\n- {id: same, title: two, task: y}\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalLoadError, match="重复 case id"):
        load_cases(cases)
    for value in ("../x", "/absolute", "a/./b", "C:\\outside"):
        with pytest.raises(EvalLoadError):
            safe_relative_path(value)


def test_confined_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "root"
    outside.mkdir()
    root.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(EvalLoadError, match="路径逃逸"):
        confined_path(root, "link/file.txt", label="test")


def test_scripted_client_emits_tool_usage_and_exhaustion():
    client = ScriptedClient(
        [
            ScriptRound.model_validate(
                {
                    "tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}],
                    "usage": {"prompt_tokens": 3},
                }
            )
        ]
    )
    events = list(client.complete_stream([]))
    assert [event.kind for event in events] == ["usage", "tool_calls"]
    assert events[1].tool_calls[0].name == "read_file"
    exhausted = list(client.complete_stream([]))
    assert exhausted[0].kind == "error"
    assert "eval_script_exhausted" in exhausted[0].text


@pytest.mark.parametrize(
    ("mode", "actual", "expected", "passes"),
    [
        ("strict", ["a", "b"], ["a", "b"], True),
        ("strict", ["b", "a"], ["a", "b"], False),
        ("unordered", ["b", "a"], ["a", "b"], True),
        ("subset", ["a"], ["a", "b"], True),
        ("subset", ["a", "a"], ["a", "b"], False),
        ("superset", ["a", "b", "c"], ["a", "b"], True),
    ],
)
def test_trajectory_modes(tmp_path, mode, actual, expected, passes):
    case = _case(
        expect={
            "trajectory": mode,
            "expected_calls": [{"name": name} for name in expected],
        }
    )
    calls = [TraceCall(name=name, arguments={}) for name in actual]
    checks = score_case(
        case,
        workspace=tmp_path,
        outcome="success",
        final="",
        calls=calls,
        permission_denials=0,
    )
    trajectory = next(check for check in checks if check.code == "trajectory")
    assert trajectory.passed is passes


def test_file_scorer_checks_content(tmp_path):
    (tmp_path / "result.txt").write_text("ready\n", encoding="utf-8")
    case = _case(expect={"files": {"result.txt": {"equals": "ready\n"}}})
    checks = score_case(
        case,
        workspace=tmp_path,
        outcome="success",
        final="done",
        calls=[],
        permission_denials=0,
    )
    assert all(check.passed for check in checks)


def test_runner_restores_cwd_and_records_permission_denial():
    before = Path.cwd()
    case = _case(
        script=[
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "../x", "content": "x"}}]},
            {"final": "blocked"},
        ],
        expect={"permission_denials": 1},
    )
    result = run_cases([case])[0]
    assert Path.cwd() == before
    assert result.metrics.permission_denials == 1
    assert result.calls[0].denied is True


def test_runner_redacts_secret_arguments_from_report(tmp_path):
    case = _case(
        mocks={"mcp_tools": [{"server": "remote", "tool": "call"}]},
        script=[
            {
                "tool_calls": [
                    {
                        "name": "mcp__remote__call",
                        "arguments": {"api_token": "must-not-appear"},
                    }
                ]
            },
            {"final": "blocked"},
        ],
    )
    result = run_cases([case])[0]
    report, _ = write_report([result], _metadata(), tmp_path)
    payload = report.read_text(encoding="utf-8")
    assert "must-not-appear" not in payload
    assert result.calls[0].arguments["api_token"] == "***REDACTED***"


def test_real_runner_closes_mcp_on_case_failure(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("active: p\nproviders:\n  p:\n    model: openai/fake\n", encoding="utf-8")
    closed = []

    class FakeManager:
        def __init__(self, *_args):
            pass

        def start(self):
            return []

        def close(self):
            closed.append(True)

    monkeypatch.setattr(runner, "MCPManager", FakeManager)
    monkeypatch.setattr(
        runner, "_run_one", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError, match="boom"):
        run_cases([_case(tags=["real"])], mode="real", config_path=config, enable_mcp=True)
    assert closed == [True]


def test_report_roundtrip_and_compare_warnings(tmp_path):
    baseline_path, _ = write_report([_result()], _metadata(), tmp_path / "baseline")
    candidate_path, _ = write_report(
        [_result("other"), _result("other", repetition=2)],
        _metadata(prompt_hash="changed", tool_schema_hash="changed"),
        tmp_path / "candidate",
    )
    loaded, summary = read_report(baseline_path)
    assert loaded[0].case_id == "sample"
    assert summary.cases == 1
    output = compare_reports(baseline_path, candidate_path, tmp_path / "compare.md")
    text = output.read_text(encoding="utf-8")
    assert "prompt hash differs" in text
    assert "tool schema hash differs" in text
    assert "case/repetition sets differ" in text
    assert "repetition counts differ" in text


def test_build_metadata_uses_explicit_feature_states():
    metadata = build_metadata(
        "scripted",
        [_result()],
        skills_enabled=True,
        mcp_enabled=True,
        compaction_enabled=True,
    )
    assert metadata.skills_enabled is True
    assert metadata.mcp_enabled is True
    assert metadata.compaction_enabled is True


def test_cli_exit_codes(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    assert main(["scripted", "--cases", str(bad)]) == 2

    report_dir = tmp_path / "report"
    cases = Path(__file__).parents[1] / "evals" / "cases"
    assert main(["scripted", "--cases", str(cases), "--report-dir", str(report_dir)]) == 0
    assert (report_dir / "results.jsonl").is_file()
