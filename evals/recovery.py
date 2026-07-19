"""M10b 可恢复执行的确定性故障注入 eval。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.config.schema import AppConfig
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.tools.registry import ToolRegistry
from evals.schema import ScriptRound
from evals.scripted_client import ScriptedClient
from evals.support import EvalToolContext, Tool, ToolResult


class SimulatedCrash(BaseException):
    pass


class CrashStore(RunStore):
    def __init__(
        self,
        base_dir: Path,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        before_save: bool = False,
    ) -> None:
        super().__init__(base_dir)
        self._predicate = predicate
        self._before_save = before_save
        self._crashed = False

    def save(self, run_id: str, document: dict[str, Any]) -> None:
        crash = not self._crashed and self._predicate(document)
        if crash and self._before_save:
            self._crashed = True
            raise SimulatedCrash()
        super().save(run_id, document)
        if crash:
            self._crashed = True
            raise SimulatedCrash()


class CountingTool(Tool):
    description = "recovery eval counter"

    def __init__(self, name: str, counts: dict[str, int]) -> None:
        self.name = name
        self._counts = counts

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def permission_requests(self, args, ctx):
        return []

    def run(self, args, ctx) -> ToolResult:
        self._counts[self.name] = self._counts.get(self.name, 0) + 1
        return ToolResult.ok(f"{self.name}-done")


@dataclass(frozen=True)
class RecoveryEvalResult:
    case_id: str
    passed: bool
    detail: str


def _config(*, max_tool_calls: int = 10) -> AppConfig:
    return AppConfig.model_validate(
        {
            "active": "eval",
            "providers": {"eval": {"model": "openai/eval-scripted"}},
            "agent": {"max_iterations": 5, "max_tool_calls": max_tool_calls},
        }
    )


def _loop(
    rounds: list[ScriptRound],
    tools: list[Tool],
    config: AppConfig,
    *,
    interactive: bool = True,
) -> tuple[AgentLoop, ScriptedClient]:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    client = ScriptedClient(rounds)
    loop = AgentLoop(
        config,
        client,
        registry,
        EvalToolContext(interactive=interactive),
        interactive=interactive,
        system_prompt="recovery-eval",
    )
    return loop, client


def _coordinator(loop: AgentLoop, store: RunStore, config: AppConfig) -> RunCoordinator:
    return RunCoordinator.create(
        store,
        task="eval task",
        provider=config.active,
        model=config.active_provider.model,
        system_prompt=loop.system_prompt,
        tool_schemas=loop.tool_schemas,
        interactive=True,
        max_iterations=config.agent.max_iterations,
        max_tool_calls=config.agent.max_tool_calls,
        max_total_tool_output_chars=config.agent.max_total_tool_output_chars,
        run_id="run-eval",
    )


def _tool_round(*names: str) -> ScriptRound:
    return ScriptRound.model_validate(
        {"tool_calls": [{"name": name, "id": f"c-{index}"} for index, name in enumerate(names)]}
    )


def _expect_crash(events) -> None:
    try:
        list(events)
    except SimulatedCrash:
        return
    raise AssertionError("预期的 checkpoint 故障未触发")


def _planned_case(root: Path) -> None:
    counts: dict[str, int] = {}
    tool = CountingTool("effect", counts)
    config = _config()
    store = CrashStore(
        root,
        lambda doc: bool(doc["tool_calls"]) and doc["tool_calls"][0]["status"] == "planned",
    )
    loop, first_client = _loop([_tool_round("effect")], [tool], config)
    _expect_crash(loop.run("eval task", coordinator=_coordinator(loop, store, config)))
    resumed, second_client = _loop([ScriptRound(final="done")], [tool], config)
    events = list(resumed.resume(RunCoordinator.load(RunStore(root), "run-eval")))
    assert counts == {"effect": 1}
    assert first_client.calls == 1 and second_client.calls == 1
    assert events[-1].kind == "final"


def _partial_batch_case(root: Path) -> None:
    counts: dict[str, int] = {}
    tools = [CountingTool("first", counts), CountingTool("second", counts)]
    config = _config()
    store = CrashStore(
        root,
        lambda doc: (
            len(doc["tool_calls"]) == 2
            and doc["tool_calls"][0]["status"] == "completed"
            and doc["tool_calls"][1]["status"] == "planned"
        ),
    )
    loop, _ = _loop([_tool_round("first", "second")], tools, config)
    _expect_crash(loop.run("eval task", coordinator=_coordinator(loop, store, config)))
    resumed, _ = _loop([ScriptRound(final="done")], tools, config)
    list(resumed.resume(RunCoordinator.load(RunStore(root), "run-eval")))
    assert counts == {"first": 1, "second": 1}


def _uncertain_case(root: Path) -> None:
    counts: dict[str, int] = {}
    tool = CountingTool("effect", counts)
    config = _config()
    store = CrashStore(
        root,
        lambda doc: bool(doc["tool_calls"]) and doc["tool_calls"][0]["status"] == "completed",
        before_save=True,
    )
    loop, _ = _loop([_tool_round("effect")], [tool], config)
    _expect_crash(loop.run("eval task", coordinator=_coordinator(loop, store, config)))
    resumed, client = _loop([], [tool], config, interactive=False)
    loaded = RunCoordinator.load(RunStore(root), "run-eval")
    events = list(resumed.resume(loaded))
    assert counts == {"effect": 1}
    assert client.calls == 0
    assert events[-1].kind == "error" and loaded.state.phase == "tool_uncertain"


def _budget_case(root: Path) -> None:
    counts: dict[str, int] = {}
    tools = [CountingTool("first", counts), CountingTool("second", counts)]
    config = _config(max_tool_calls=1)
    store = CrashStore(
        root,
        lambda doc: (
            doc["phase"] == "model_pending"
            and doc["iteration"] == 1
            and doc["tool_budget"]["used_calls"] == 1
        ),
    )
    loop, _ = _loop([_tool_round("first")], tools, config)
    _expect_crash(loop.run("eval task", coordinator=_coordinator(loop, store, config)))
    resumed, _ = _loop([_tool_round("second")], tools, config)
    events = list(resumed.resume(RunCoordinator.load(RunStore(root), "run-eval")))
    assert counts == {"first": 1}
    assert events[-1].kind == "error" and "预算" in events[-1].text


def run_recovery_evals() -> list[RecoveryEvalResult]:
    cases = [
        ("planned_resume", _planned_case),
        ("partial_batch_no_replay", _partial_batch_case),
        ("uncertain_side_effect_pauses", _uncertain_case),
        ("budget_survives_restart", _budget_case),
    ]
    results: list[RecoveryEvalResult] = []
    for case_id, run_case in cases:
        with TemporaryDirectory(prefix=f"assistant-agent-{case_id}-") as raw_root:
            try:
                run_case(Path(raw_root))
            except BaseException as exc:  # eval 必须把故障收敛为可报告结果
                results.append(RecoveryEvalResult(case_id, False, f"{type(exc).__name__}: {exc}"))
            else:
                results.append(RecoveryEvalResult(case_id, True, "ok"))
    return results
