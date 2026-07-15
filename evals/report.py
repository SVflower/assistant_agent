"""Eval JSONL/Markdown 报告、聚合指标与 A/B 比较。"""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from assistant_agent.config.schema import AppConfig
from evals.schema import CaseResult, RunMetadata, RunSummary


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_metadata(
    mode: Literal["scripted", "real"],
    results: list[CaseResult],
    config: AppConfig | None = None,
    *,
    skills_enabled: bool = False,
    mcp_enabled: bool = False,
    compaction_enabled: bool = False,
) -> RunMetadata:
    provider = config.active if config is not None else "scripted"
    model = config.active_provider.model if config is not None else "scripted"
    return RunMetadata(
        mode=mode,
        model_capability=mode == "real",
        git_commit=_git_commit(),
        python=platform.python_version(),
        platform=platform.platform(),
        provider=provider,
        model=model,
        prompt_hash=_hashes(result.prompt_hash for result in results),
        tool_schema_hash=_hashes(result.tool_schema_hash for result in results),
        permission_mode="per-case",
        skills_enabled=skills_enabled,
        mcp_enabled=mcp_enabled,
        compaction_enabled=compaction_enabled,
        started_at=datetime.now().isoformat(timespec="seconds"),
    )


def _hashes(values: Any) -> str:
    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else "mixed:" + ",".join(unique)


def summarize(results: list[CaseResult], metadata: RunMetadata) -> RunSummary:
    count = len(results)
    passed = sum(result.passed for result in results)
    tool_calls = sum(result.metrics.tool_calls for result in results)
    illegal = sum(result.metrics.illegal_tool_calls for result in results)
    repeated = sum(result.metrics.repeated_tool_calls for result in results)
    return RunSummary(
        metadata=metadata,
        cases=count,
        passed=passed,
        success_rate=passed / count if count else 0,
        tool_calls=tool_calls,
        illegal_tool_rate=illegal / tool_calls if tool_calls else 0,
        repeat_rate=repeated / tool_calls if tool_calls else 0,
        input_tokens=sum(result.metrics.input_tokens for result in results),
        output_tokens=sum(result.metrics.output_tokens for result in results),
        duration_ms=sum(result.metrics.duration_ms for result in results),
    )


def write_report(
    results: list[CaseResult],
    metadata: RunMetadata,
    report_dir: str | Path,
) -> tuple[Path, Path]:
    root = Path(report_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    jsonl = root / "results.jsonl"
    markdown = root / "summary.md"
    summary = summarize(results, metadata)
    lines = [
        json.dumps({"type": "case", **result.model_dump()}, ensure_ascii=False)
        for result in results
    ]
    lines.append(json.dumps(summary.model_dump(), ensure_ascii=False))
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    markdown.write_text(_markdown(results, summary), encoding="utf-8")
    return jsonl, markdown


def _markdown(results: list[CaseResult], summary: RunSummary) -> str:
    meta = summary.metadata
    warning = (
        "本报告使用真实模型，结果可能波动。"
        if meta.model_capability
        else "本报告使用 scripted client，只验证框架轨迹与护栏，不代表模型能力。"
    )
    lines = [
        "# Assistant Agent Eval Report",
        "",
        f"> {warning}",
        "",
        f"- mode: `{meta.mode}`",
        f"- provider/model: `{meta.provider}` / `{meta.model}`",
        f"- git: `{meta.git_commit}`",
        f"- cases: {summary.passed}/{summary.cases} passed ({summary.success_rate:.1%})",
        f"- tool calls: {summary.tool_calls}",
        f"- illegal tool rate: {summary.illegal_tool_rate:.1%}",
        f"- repeat rate: {summary.repeat_rate:.1%}",
        f"- tokens: in={summary.input_tokens}, out={summary.output_tokens}",
        "",
        "| Case | Run | Result | Calls | Denials | Duration |",
        "|---|---:|:---:|---:|---:|---:|",
    ]
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        lines.append(
            f"| `{result.case_id}` | {result.repetition} | {mark} | "
            f"{result.metrics.tool_calls} | {result.metrics.permission_denials} | "
            f"{result.metrics.duration_ms} ms |"
        )
    failed = [result for result in results if not result.passed]
    if failed:
        lines.extend(["", "## Failures", ""])
        for result in failed:
            lines.append(f"### {result.case_id} (run {result.repetition})")
            for check in result.checks:
                if not check.passed:
                    lines.append(f"- `{check.code}`: {check.message}")
            if result.error:
                lines.append(f"- `runner_error`: {result.error}")
    return "\n".join(lines) + "\n"


def read_report(path: str | Path) -> tuple[list[CaseResult], RunSummary]:
    results: list[CaseResult] = []
    summary: RunSummary | None = None
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"报告第 {number} 行不是合法 JSON") from exc
        if record.get("type") == "case":
            record.pop("type", None)
            results.append(CaseResult.model_validate(record))
        elif record.get("type") == "summary":
            summary = RunSummary.model_validate(record)
    if summary is None:
        raise ValueError("报告缺少 summary")
    return results, summary


def compare_reports(
    baseline_path: str | Path,
    candidate_path: str | Path,
    output_path: str | Path,
) -> Path:
    baseline, baseline_summary = read_report(baseline_path)
    candidate, candidate_summary = read_report(candidate_path)
    baseline_map = {(result.case_id, result.repetition): result for result in baseline}
    candidate_map = {(result.case_id, result.repetition): result for result in candidate}
    all_keys = sorted(set(baseline_map) | set(candidate_map))
    lines = [
        "# Eval Comparison",
        "",
        "> 变化仅为描述性 delta，不代表统计显著。",
        "",
        f"- baseline: `{baseline_summary.metadata.git_commit}`",
        f"- candidate: `{candidate_summary.metadata.git_commit}`",
        f"- success rate: {baseline_summary.success_rate:.1%} -> "
        f"{candidate_summary.success_rate:.1%} "
        f"({candidate_summary.success_rate - baseline_summary.success_rate:+.1%})",
        f"- tool calls: {baseline_summary.tool_calls} -> {candidate_summary.tool_calls} "
        f"({candidate_summary.tool_calls - baseline_summary.tool_calls:+d})",
        f"- input tokens: {baseline_summary.input_tokens} -> {candidate_summary.input_tokens}",
        f"- output tokens: {baseline_summary.output_tokens} -> {candidate_summary.output_tokens}",
        "",
        "| Case | Run | Baseline | Candidate | Calls delta |",
        "|---|---:|:---:|:---:|---:|",
    ]
    for case_id, repetition in all_keys:
        before = baseline_map.get((case_id, repetition))
        after = candidate_map.get((case_id, repetition))
        before_label = "missing" if before is None else ("PASS" if before.passed else "FAIL")
        after_label = "missing" if after is None else ("PASS" if after.passed else "FAIL")
        call_delta = (after.metrics.tool_calls if after else 0) - (
            before.metrics.tool_calls if before else 0
        )
        lines.append(
            f"| `{case_id}` | {repetition} | {before_label} | {after_label} | {call_delta:+d} |"
        )
    if baseline_summary.metadata.prompt_hash != candidate_summary.metadata.prompt_hash:
        lines.extend(["", "- Warning: prompt hash differs."])
    if baseline_summary.metadata.tool_schema_hash != candidate_summary.metadata.tool_schema_hash:
        lines.append("- Warning: tool schema hash differs.")
    baseline_keys = set(baseline_map)
    candidate_keys = set(candidate_map)
    if baseline_keys != candidate_keys:
        lines.append("- Warning: case/repetition sets differ; missing rows are shown explicitly.")
    baseline_counts = _repetition_counts(baseline)
    candidate_counts = _repetition_counts(candidate)
    if baseline_counts != candidate_counts:
        lines.append("- Warning: repetition counts differ; aggregate deltas are descriptive only.")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def _repetition_counts(results: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.case_id] = counts.get(result.case_id, 0) + 1
    return counts
