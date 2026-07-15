"""可解释的确定性行为评分器。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from assistant_agent.obs import sanitize_for_display
from evals.loader import EvalLoadError, confined_path
from evals.schema import CheckResult, EvalCase, ExpectedToolCall, TraceCall


def _check(code: str, passed: bool, success: str, failure: str) -> CheckResult:
    return CheckResult(code=code, passed=passed, message=success if passed else failure)


def _matches(actual: TraceCall, expected: ExpectedToolCall) -> bool:
    return actual.name == expected.name and (
        expected.arguments is None or actual.arguments == expected.arguments
    )


def _contains_all(available: list[TraceCall], required: list[ExpectedToolCall]) -> bool:
    remaining = list(available)
    for expected in required:
        index = next((i for i, actual in enumerate(remaining) if _matches(actual, expected)), None)
        if index is None:
            return False
        remaining.pop(index)
    return True


def _is_subset(actual: list[TraceCall], expected: list[ExpectedToolCall]) -> bool:
    remaining = list(expected)
    for call in actual:
        index = next((i for i, wanted in enumerate(remaining) if _matches(call, wanted)), None)
        if index is None:
            return False
        remaining.pop(index)
    return True


def _trajectory_passes(case: EvalCase, calls: list[TraceCall]) -> bool:
    expected = case.expect.expected_calls
    if not expected:
        return True
    mode = case.expect.trajectory
    if mode == "strict":
        return len(calls) == len(expected) and all(
            _matches(actual, wanted) for actual, wanted in zip(calls, expected, strict=True)
        )
    if mode == "unordered":
        return len(calls) == len(expected) and _contains_all(calls, expected)
    if mode == "subset":
        return _is_subset(calls, expected)
    return _contains_all(calls, expected)


def score_case(
    case: EvalCase,
    *,
    workspace: Path,
    outcome: str,
    final: str,
    calls: list[TraceCall],
    permission_denials: int,
) -> list[CheckResult]:
    expected = case.expect
    checks = [
        _check(
            "outcome",
            outcome == expected.outcome,
            f"outcome={outcome}",
            f"期望 outcome={expected.outcome}，实际为 {outcome}",
        ),
        _check(
            "trajectory",
            _trajectory_passes(case, calls),
            f"轨迹满足 {expected.trajectory}",
            f"轨迹不满足 {expected.trajectory}："
            f"{[(c.name, sanitize_for_display(c.arguments)) for c in calls]}",
        ),
    ]
    names = [call.name for call in calls]
    missing = sorted(set(expected.required_tools) - set(names))
    illegal = [name for name in names if name in set(expected.forbidden_tools)]
    checks.extend(
        [
            _check("required_tools", not missing, "必需工具均已调用", f"缺少工具：{missing}"),
            _check("forbidden_tools", not illegal, "未调用禁止工具", f"调用了禁止工具：{illegal}"),
        ]
    )
    if expected.max_tool_calls is not None:
        checks.append(
            _check(
                "max_tool_calls",
                len(calls) <= expected.max_tool_calls,
                f"工具调用 {len(calls)}/{expected.max_tool_calls}",
                f"工具调用超限：{len(calls)}/{expected.max_tool_calls}",
            )
        )
    if expected.permission_denials is not None:
        checks.append(
            _check(
                "permission_denials",
                permission_denials == expected.permission_denials,
                f"权限拒绝数={permission_denials}",
                f"期望权限拒绝 {expected.permission_denials}，实际 {permission_denials}",
            )
        )
    if expected.final_exact is not None:
        checks.append(
            _check(
                "final_exact",
                final == expected.final_exact,
                "最终文本精确匹配",
                "最终文本不匹配",
            )
        )
    for needle in expected.final_contains:
        checks.append(
            _check(
                f"final_contains:{needle}",
                needle in final,
                f"最终文本包含 {needle!r}",
                f"最终文本缺少 {needle!r}",
            )
        )
    for needle in expected.final_not_contains:
        checks.append(
            _check(
                f"final_not_contains:{needle}",
                needle not in final,
                f"最终文本不含 {needle!r}",
                f"最终文本不应包含 {needle!r}",
            )
        )
    for relative, assertion in expected.files.items():
        try:
            path = confined_path(workspace, relative, label="文件断言")
        except EvalLoadError as exc:
            checks.append(CheckResult(code=f"file_path:{relative}", passed=False, message=str(exc)))
            continue
        exists = path.is_file()
        if assertion.exists is not None:
            checks.append(
                _check(
                    f"file_exists:{relative}",
                    exists is assertion.exists,
                    f"文件存在状态={exists}",
                    f"文件存在状态期望 {assertion.exists}，实际 {exists}",
                )
            )
        if assertion.equals is not None or assertion.contains or assertion.not_contains:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                checks.append(
                    CheckResult(code=f"file_read:{relative}", passed=False, message=str(exc))
                )
                continue
            if assertion.equals is not None:
                checks.append(
                    _check(
                        f"file_equals:{relative}",
                        content == assertion.equals,
                        "文件内容精确匹配",
                        f"文件内容不匹配（实际长度 {len(content)}）",
                    )
                )
            for needle in assertion.contains:
                checks.append(
                    _check(
                        f"file_contains:{relative}:{needle}",
                        needle in content,
                        f"文件包含 {needle!r}",
                        f"文件缺少 {needle!r}",
                    )
                )
            for needle in assertion.not_contains:
                checks.append(
                    _check(
                        f"file_not_contains:{relative}:{needle}",
                        needle not in content,
                        f"文件不含 {needle!r}",
                        f"文件不应包含 {needle!r}",
                    )
                )
    return checks


def call_signature(call: TraceCall) -> str:
    return json.dumps([call.name, call.arguments], sort_keys=True, ensure_ascii=False, default=str)


def repeated_call_count(calls: list[TraceCall]) -> int:
    counts = Counter(call_signature(call) for call in calls)
    return sum(max(count - 1, 0) for count in counts.values())
