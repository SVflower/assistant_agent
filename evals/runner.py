"""Scripted/真实 provider 共用的串行 eval runner。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from assistant_agent.agent.loop import AgentLoop, StepEvent
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.config.loader import load_config
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMClient
from assistant_agent.mcp import MCPManager
from assistant_agent.mcp.tool import MCPTool
from assistant_agent.obs import NullLogger
from assistant_agent.obs.redaction import redact_text, sanitize_args, truncate_text
from assistant_agent.skills import LoadSkillTool, SkillStore
from assistant_agent.tools.base import ConfirmChoice, ToolContext
from assistant_agent.tools.permissions import Capability, PermissionRule
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.registry import build_default_registry
from evals.integrations import discover_configured_skills, register_case_mocks
from evals.loader import fixture_workspace
from evals.schema import (
    CaseMetrics,
    CaseResult,
    CheckResult,
    EvalCase,
    TraceCall,
)
from evals.scorers import repeated_call_count, score_case
from evals.scripted_client import ScriptedClient


class EvalAuditLogger(NullLogger):
    def __init__(self) -> None:
        self.permission_denials = 0
        self.permission_events: list[dict[str, Any]] = []

    def permission_decision(self, **event: Any) -> None:
        self.permission_events.append(event)
        if event.get("decision") == "deny":
            self.permission_denials += 1


class ConfirmationCounter:
    def __init__(self, choice: ConfirmChoice) -> None:
        self.choice = choice
        self.calls = 0

    def __call__(self, _message: str) -> ConfirmChoice:
        self.calls += 1
        return self.choice


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _sha(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _policy(case: EvalCase, workspace: Path) -> PermissionPolicy:
    rules: list[PermissionRule] = []
    for rule in case.permissions.rules:
        target = rule.target
        if rule.capability.startswith("filesystem.") and target != "*":
            candidate = Path(target)
            if not candidate.is_absolute():
                target = str((workspace / candidate).resolve())
        rules.append(
            PermissionRule(
                effect=rule.effect,
                capability=Capability(rule.capability),
                target=target,
                tool=rule.tool,
            )
        )
    return PermissionPolicy(
        mode=case.permissions.mode,
        rules=rules,
        sensitive_paths=[workspace / ".eval-sensitive"],
    )


def _config(case: EvalCase, base: AppConfig | None = None) -> AppConfig:
    if base is None:
        config = AppConfig.model_validate(
            {"active": "eval", "providers": {"eval": {"model": "openai/eval-scripted"}}}
        )
    else:
        config = base.model_copy(deep=True)
    budget = case.budget
    config.agent.max_iterations = budget.max_iterations
    config.agent.max_tool_calls = budget.max_tool_calls
    config.agent.max_total_tool_output_chars = budget.max_total_tool_output_chars
    config.agent.max_context_tokens = budget.max_context_tokens
    config.agent.max_history_messages = budget.max_history_messages
    config.agent.reserved_output_tokens = budget.reserved_output_tokens
    config.agent.compaction.enabled = budget.compaction_enabled
    config.agent.compaction.threshold = budget.compaction_threshold
    config.agent.compaction.keep_recent_turns = budget.compaction_keep_recent_turns
    config.tools.max_output_chars = budget.max_output_chars
    return config


def _collect(
    events: list[StepEvent],
) -> tuple[Literal["success", "error", "interrupted"], str, list[TraceCall], int, int]:
    outcome: Literal["success", "error", "interrupted"] = "error"
    final = ""
    calls: list[TraceCall] = []
    pending: list[TraceCall] = []
    input_tokens = 0
    output_tokens = 0
    for event in events:
        if event.kind == "tool_call":
            call = TraceCall(name=event.tool_name, arguments=event.tool_args or {})
            calls.append(call)
            pending.append(call)
        elif event.kind == "tool_result" and pending:
            call = pending.pop(0)
            call.output = event.text
            call.is_error = event.is_error
            call.denied = "[permission_denied]" in event.text
        elif event.kind == "usage" and event.usage:
            input_tokens += event.usage.get("prompt_tokens", event.usage.get("input_tokens", 0))
            output_tokens += event.usage.get(
                "completion_tokens", event.usage.get("output_tokens", 0)
            )
        elif event.kind == "final":
            outcome = "success"
            final = event.text
        elif event.kind == "error":
            outcome = "error"
            final = event.text
        elif event.kind == "interrupted":
            outcome = "interrupted"
            final = event.text
    return outcome, final, calls, input_tokens, output_tokens


def _report_calls(calls: list[TraceCall]) -> list[TraceCall]:
    return [
        call.model_copy(
            update={
                "arguments": sanitize_args(call.arguments, 500),
                "output": truncate_text(redact_text(call.output), 1_000),
            }
        )
        for call in calls
    ]


def _run_one(
    case: EvalCase,
    *,
    mode: Literal["scripted", "real"],
    repetition: int,
    base_config: AppConfig | None,
    provider: str | None,
    skill_store: SkillStore | None,
    mcp_tools: list[MCPTool],
) -> CaseResult:
    with fixture_workspace(case) as root, _working_directory(root):
        config = _config(case, base_config)
        if provider is not None:
            if provider not in config.providers:
                raise ValueError(f"未知 provider：{provider}")
            config.active = provider
        registry = build_default_registry()
        mocked_skills = register_case_mocks(case, root, registry)
        configured_skills = skill_store.list() if skill_store is not None else []
        if configured_skills and registry.get("load_skill") is None:
            assert skill_store is not None
            registry.register(LoadSkillTool(skill_store))
        for tool in mcp_tools:
            registry.register(tool)
        visible_skills = [meta for meta in [*configured_skills, *mocked_skills] if meta.trusted]
        system_prompt = build_system_prompt(
            interactive=mode == "scripted",
            skills=[(meta.name, f"[{meta.source}] {meta.description}") for meta in visible_skills]
            or None,
        )
        logger = EvalAuditLogger()
        confirmation = ConfirmationCounter(case.permissions.confirm)
        context = ToolContext(
            workspace_root=root,
            logger=logger,
            permission_policy=_policy(case, root),
            confirm=confirmation,
            interactive=mode == "scripted",
            max_output_chars=config.tools.max_output_chars,
        )
        client = (
            ScriptedClient(case.script) if mode == "scripted" else LLMClient(config.active_provider)
        )
        loop = AgentLoop(
            config,
            client,  # type: ignore[arg-type]
            registry,
            context,
            interactive=mode == "scripted",
            system_prompt=system_prompt,
        )
        if case.history:
            loop.load_history(case.history)
        started = time.perf_counter()
        error = ""
        try:
            events = list(loop.run(case.task))
        except Exception as exc:  # runner 必须把单 case 基础设施错误收进报告
            events = []
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = int((time.perf_counter() - started) * 1000)
        outcome, final, calls, input_tokens, output_tokens = _collect(events)
        checks = score_case(
            case,
            workspace=root,
            outcome=outcome,
            final=final,
            calls=calls,
            permission_denials=logger.permission_denials,
        )
        if error:
            checks.append(CheckResult(code="runner_error", passed=False, message=error))
        illegal_count = sum(call.name in set(case.expect.forbidden_tools) for call in calls)
        metrics = CaseMetrics(
            tool_calls=len(calls),
            illegal_tool_calls=illegal_count,
            repeated_tool_calls=repeated_call_count(calls),
            permission_denials=logger.permission_denials,
            confirmations=confirmation.calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )
        return CaseResult(
            case_id=case.id,
            mode=mode,
            repetition=repetition,
            passed=not error and all(check.passed for check in checks),
            outcome=outcome,
            final=truncate_text(redact_text(final), 4_000),
            calls=_report_calls(calls),
            checks=[
                check.model_copy(
                    update={"message": truncate_text(redact_text(check.message), 1_000)}
                )
                for check in checks
            ],
            metrics=metrics,
            error=truncate_text(redact_text(error), 1_000),
            prompt_hash=_sha(system_prompt),
            tool_schema_hash=_sha(registry.schemas()),
        )


def run_cases(
    cases: list[EvalCase],
    *,
    mode: Literal["scripted", "real"] = "scripted",
    config_path: str | Path | None = None,
    provider: str | None = None,
    repeat: int = 1,
    enable_skills: bool = False,
    enable_mcp: bool = False,
) -> list[CaseResult]:
    if repeat < 1:
        raise ValueError("repeat 必须 >= 1")
    if mode == "scripted":
        selected = [case for case in cases if case.script]
        base_config = None
    else:
        if config_path is None:
            raise ValueError("real eval 必须提供 --config")
        selected = [case for case in cases if case.supports_real]
        base_config = load_config(config_path)
    if not selected:
        raise ValueError(f"没有可运行的 {mode} case")
    if mode == "scripted" and (enable_skills or enable_mcp):
        raise ValueError("scripted eval 不接入外部 Skills/MCP")
    skill_store = (
        discover_configured_skills(base_config)
        if enable_skills and base_config is not None
        else None
    )
    manager = MCPManager(base_config.mcp, NullLogger()) if enable_mcp and base_config else None
    mcp_tools = manager.start() if manager is not None else []
    results: list[CaseResult] = []
    try:
        for repetition in range(1, repeat + 1):
            for case in selected:
                results.append(
                    _run_one(
                        case,
                        mode=mode,
                        repetition=repetition,
                        base_config=base_config,
                        provider=provider,
                        skill_store=skill_store,
                        mcp_tools=mcp_tools,
                    )
                )
        return results
    finally:
        if manager is not None:
            manager.close()
