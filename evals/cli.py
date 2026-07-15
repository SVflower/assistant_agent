"""Eval 命令行：scripted、real、compare。"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from assistant_agent.config.loader import load_config
from evals.loader import load_cases
from evals.report import build_metadata, compare_reports, write_report
from evals.runner import run_cases


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scripted", "real"):
        command = sub.add_parser(name)
        command.add_argument("--cases", default="evals/cases")
        command.add_argument("--case", action="append", default=[])
        command.add_argument("--tag", action="append", default=[])
        command.add_argument("--repeat", type=int, default=1)
        command.add_argument("--report-dir")
        if name == "real":
            command.add_argument("--config", required=True)
            command.add_argument("--provider")
            command.add_argument("--skills", action="store_true")
            command.add_argument("--mcp", action="store_true")
    compare = sub.add_parser("compare")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--output", default="evals/reports/compare.md")
    return parser


def _filter(cases, ids: list[str], tags: list[str]):
    selected = cases
    if ids:
        wanted = set(ids)
        selected = [case for case in selected if case.id in wanted]
        missing = sorted(wanted - {case.id for case in selected})
        if missing:
            raise ValueError(f"未知 case：{missing}")
    if tags:
        selected = [case for case in selected if set(tags).issubset(set(case.tags))]
    if not selected:
        raise ValueError("case 过滤后为空")
    return selected


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "compare":
        output = compare_reports(args.baseline, args.candidate, args.output)
        print(output)
        return 0
    try:
        cases = _filter(load_cases(args.cases), args.case, args.tag)
        config_path = getattr(args, "config", None)
        provider = getattr(args, "provider", None)
        results = run_cases(
            cases,
            mode=args.command,
            config_path=config_path,
            provider=provider,
            repeat=args.repeat,
            enable_skills=getattr(args, "skills", False),
            enable_mcp=getattr(args, "mcp", False),
        )
        config = load_config(config_path) if args.command == "real" else None
        if config is not None and provider:
            config.active = provider
        executed_cases = (
            [case for case in cases if case.script]
            if args.command == "scripted"
            else [case for case in cases if case.supports_real]
        )
        metadata = build_metadata(
            args.command,
            results,
            config,
            skills_enabled=getattr(args, "skills", False)
            and bool(config and config.skills.enabled),
            mcp_enabled=getattr(args, "mcp", False)
            and bool(config and config.mcp.enabled and config.mcp.servers),
            compaction_enabled=any(case.budget.compaction_enabled for case in executed_cases),
        )
        report_dir = args.report_dir or (
            Path("evals/reports")
            / f"{datetime.now():%Y%m%d-%H%M%S}-{args.command}-{metadata.provider}"
        )
        jsonl, markdown = write_report(results, metadata, report_dir)
        print(jsonl)
        print(markdown)
        return 0 if all(result.passed for result in results) else 1
    except (OSError, ValueError) as exc:
        print(f"eval error: {exc}")
        return 2
