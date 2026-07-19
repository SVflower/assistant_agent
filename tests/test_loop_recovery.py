"""真实 checkpoint 边界上的 AgentLoop crash/resume 测试。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.run.budgets import ContinuationController
from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.agent.run.failures import ContinuationResult
from assistant_agent.config.schema import AppConfig, ContinuationConfig
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.providers.ports import StreamEvent, ToolCall
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.registry import ToolRegistry
from tests.support import Tool, ToolBudget, ToolContextFixture, ToolResult


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
        self.predicate = predicate
        self.before_save = before_save
        self.crashed = False

    def save(self, run_id: str, document: dict[str, Any]) -> None:
        should_crash = not self.crashed and self.predicate(document)
        if should_crash and self.before_save:
            self.crashed = True
            raise SimulatedCrash()
        super().save(run_id, document)
        if should_crash:
            self.crashed = True
            raise SimulatedCrash()


class ScriptedClient:
    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
        self.rounds = rounds
        self.calls = 0

    def complete_stream(self, messages, tools=None) -> Iterator[StreamEvent]:
        events = self.rounds[self.calls]
        self.calls += 1
        yield from events


class CountingTool(Tool):
    description = "test"

    def __init__(
        self,
        name: str,
        counts: dict[str, int],
        *,
        readonly_target: Path | None = None,
    ) -> None:
        self.name = name
        self.counts = counts
        self.readonly_target = readonly_target

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def permission_requests(self, args, ctx):
        if self.readonly_target is None:
            return []
        return [
            PermissionRequest(
                self.name,
                Capability.FILESYSTEM_READ,
                str(self.readonly_target),
                "read",
            )
        ]

    def run(self, args, ctx) -> ToolResult:
        self.counts[self.name] = self.counts.get(self.name, 0) + 1
        return ToolResult.ok(f"{self.name}-done")


class UnknownOutcomeTool(CountingTool):
    def run(self, args, ctx) -> ToolResult:
        self.counts[self.name] = self.counts.get(self.name, 0) + 1
        return ToolResult.error(
            "transport failed",
            code="mcp_outcome_unknown",
            retryable=False,
        )


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "active": "test",
            "providers": {"test": {"model": "openai/fake"}},
            "agent": {"max_iterations": 5, "max_tool_calls": 10},
        }
    )


def _loop(client, tools: list[Tool], *, interactive: bool = True, workspace=None):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentLoop(
        _config(),
        client,
        registry,
        ToolContextFixture(
            interactive=interactive,
            workspace_root=workspace or Path.cwd(),
            confirm=lambda _message: "allow",
        ),
        interactive=interactive,
        system_prompt="sys",
    )


def _coordinator(loop: AgentLoop, store: RunStore, task: str = "task") -> RunCoordinator:
    cfg = _config()
    return RunCoordinator.create(
        store,
        task=task,
        provider=cfg.active,
        model=cfg.active_provider.model,
        system_prompt=loop.system_prompt,
        tool_schemas=loop.tool_schemas,
        interactive=True,
        max_iterations=cfg.agent.max_iterations,
        max_tool_calls=cfg.agent.max_tool_calls,
        max_total_tool_output_chars=cfg.agent.max_total_tool_output_chars,
        run_id="run-1",
    )


def _tool_round(*names: str) -> list[StreamEvent]:
    return [
        StreamEvent(
            kind="tool_calls",
            tool_calls=[
                ToolCall(id=f"c{i}", name=name, arguments={}) for i, name in enumerate(names)
            ],
        )
    ]


def test_budget_extension_checkpoint_is_idempotent(tmp_path):
    loop = _loop(ScriptedClient([]), [])
    coordinator = _coordinator(loop, RunStore(tmp_path))
    budget = ToolBudget(max_calls=1, max_total_output_chars=100)
    coordinator.initialize([], None, budget)

    assert coordinator.extend_budget(
        request_id="continue-1",
        resource="tool_calls",
        current_limit=1,
        new_limit=2,
        budget=budget,
    )
    loaded = RunCoordinator.load(RunStore(tmp_path), coordinator.run_id)
    restored = loaded.restore_tool_context(ToolContextFixture())

    assert loaded.extend_budget(
        request_id="continue-1",
        resource="tool_calls",
        current_limit=2,
        new_limit=3,
        budget=restored,
    )
    assert restored.max_calls == 2
    assert loaded.state.tool_call_continuation.extension_count == 1
    assert len(loaded.state.continuation_decisions) == 1


def test_resumed_continuation_uses_checkpoint_limits_not_new_config(tmp_path):
    loop = _loop(ScriptedClient([]), [])
    coordinator = RunCoordinator.create(
        RunStore(tmp_path),
        task="task",
        provider="test",
        model="openai/fake",
        system_prompt=loop.system_prompt,
        tool_schemas=loop.tool_schemas,
        interactive=True,
        max_iterations=5,
        max_tool_calls=1,
        max_total_tool_output_chars=100,
        tool_call_increment=2,
        max_tool_calls_hard=3,
        run_id="run-1",
    )
    budget = ToolBudget(max_calls=1, max_total_output_chars=100)
    coordinator.initialize([], None, budget)
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    prompts = []
    controller = ContinuationController(
        ContinuationConfig(tool_call_increment=100, max_tool_calls_hard=200),
        lambda prompt: prompts.append(prompt) or ContinuationResult("saved-boundary", True),
        None,
    )

    new_limit = controller.request(
        "tool_calls",
        used=1,
        limit=1,
        budget=loaded.restore_tool_context(ToolContextFixture()),
        coordinator=loaded,
    )

    assert new_limit == 3
    assert (prompts[0].suggested_increment, prompts[0].hard_limit) == (2, 3)


def test_unknown_tool_outcome_pauses_and_uses_recovery_path(tmp_path):
    counts: dict[str, int] = {}
    tool = UnknownOutcomeTool("remote_write", counts)
    first_loop = _loop(ScriptedClient([_tool_round("remote_write")]), [tool])
    coordinator = _coordinator(first_loop, RunStore(tmp_path), task="write")

    events = list(first_loop.run("write", coordinator=coordinator))

    result = next(event for event in events if event.kind == "tool_result")
    assert result.failure is not None and result.failure.unknown_side_effect is True
    assert coordinator.state.status == "paused"
    assert coordinator.state.phase == "tool_uncertain"
    assert coordinator.state.failure is not None
    assert coordinator.state.failure.terminal_status == "paused"
    assert coordinator.state.tool_calls[0].status == "started"

    loaded = RunCoordinator.load(RunStore(tmp_path), coordinator.run_id)
    resumed_loop = _loop(
        ScriptedClient([[StreamEvent(kind="content", text="recovered")]]),
        [tool],
    )
    resumed = list(resumed_loop.resume(loaded, recovery_check=lambda _call: "skip"))

    assert resumed[-1].kind == "final"
    assert counts == {"remote_write": 1}


def test_saved_tool_plan_resumes_without_recalling_model(tmp_path):
    counts: dict[str, int] = {}
    tool = CountingTool("effect", counts)
    store = CrashStore(
        tmp_path,
        lambda doc: (
            doc["phase"] == "tools_pending"
            and doc["tool_calls"]
            and doc["tool_calls"][0]["status"] == "planned"
        ),
    )
    first_client = ScriptedClient([_tool_round("effect")])
    first_loop = _loop(first_client, [tool])
    coordinator = _coordinator(first_loop, store)

    with pytest.raises(SimulatedCrash):
        list(first_loop.run("task", coordinator=coordinator))
    assert counts == {}
    assert first_client.calls == 1

    resumed_client = ScriptedClient([[StreamEvent(kind="content", text="done")]])
    resumed_loop = _loop(resumed_client, [tool])
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    events = list(resumed_loop.resume(loaded))
    assert counts == {"effect": 1}
    assert resumed_client.calls == 1
    assert events[-1].kind == "final"


def test_partial_batch_does_not_replay_completed_call(tmp_path):
    counts: dict[str, int] = {}
    tools = [CountingTool("first", counts), CountingTool("second", counts)]
    store = CrashStore(
        tmp_path,
        lambda doc: (
            len(doc["tool_calls"]) == 2
            and doc["tool_calls"][0]["status"] == "completed"
            and doc["tool_calls"][1]["status"] == "planned"
        ),
    )
    first_loop = _loop(ScriptedClient([_tool_round("first", "second")]), tools)
    with pytest.raises(SimulatedCrash):
        list(first_loop.run("task", coordinator=_coordinator(first_loop, store)))
    assert counts == {"first": 1}

    resumed_loop = _loop(ScriptedClient([[StreamEvent(kind="content", text="done")]]), tools)
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    list(resumed_loop.resume(loaded))
    assert counts == {"first": 1, "second": 1}


def test_unknown_side_effect_stays_paused_noninteractive(tmp_path):
    counts: dict[str, int] = {}
    tool = CountingTool("effect", counts)
    store = CrashStore(
        tmp_path,
        lambda doc: doc["tool_calls"] and doc["tool_calls"][0]["status"] == "completed",
        before_save=True,
    )
    first_loop = _loop(ScriptedClient([_tool_round("effect")]), [tool])
    with pytest.raises(SimulatedCrash):
        list(first_loop.run("task", coordinator=_coordinator(first_loop, store)))
    assert counts == {"effect": 1}

    resumed_client = ScriptedClient([[StreamEvent(kind="content", text="unused")]])
    resumed_loop = _loop(resumed_client, [tool], interactive=False)
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    events = list(resumed_loop.resume(loaded))
    assert events[-1].kind == "error"
    assert counts == {"effect": 1}
    assert resumed_client.calls == 0
    assert loaded.state.phase == "tool_uncertain"


def test_unknown_side_effect_requires_explicit_retry(tmp_path):
    counts: dict[str, int] = {}
    tool = CountingTool("effect", counts)
    store = CrashStore(
        tmp_path,
        lambda doc: doc["tool_calls"] and doc["tool_calls"][0]["status"] == "completed",
        before_save=True,
    )
    first_loop = _loop(ScriptedClient([_tool_round("effect")]), [tool])
    with pytest.raises(SimulatedCrash):
        list(first_loop.run("task", coordinator=_coordinator(first_loop, store)))

    resumed_loop = _loop(ScriptedClient([[StreamEvent(kind="content", text="done")]]), [tool])
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    list(resumed_loop.resume(loaded, recovery_check=lambda _call: "retry"))
    assert counts == {"effect": 2}


def test_readonly_started_call_retries_automatically(tmp_path):
    counts: dict[str, int] = {}
    target = tmp_path / "read.txt"
    target.write_text("x", encoding="utf-8")
    tool = CountingTool("reader", counts, readonly_target=target)
    store = CrashStore(
        tmp_path / "runs",
        lambda doc: doc["tool_calls"] and doc["tool_calls"][0]["status"] == "completed",
        before_save=True,
    )
    first_loop = _loop(ScriptedClient([_tool_round("reader")]), [tool], workspace=tmp_path)
    with pytest.raises(SimulatedCrash):
        list(first_loop.run("task", coordinator=_coordinator(first_loop, store)))

    resumed_loop = _loop(
        ScriptedClient([[StreamEvent(kind="content", text="done")]]),
        [tool],
        workspace=tmp_path,
    )
    loaded = RunCoordinator.load(RunStore(tmp_path / "runs"), "run-1")
    list(resumed_loop.resume(loaded))
    assert counts == {"reader": 2}


def test_model_error_is_structured_failed_terminal(tmp_path):
    first_client = ScriptedClient([[StreamEvent(kind="error", text="connection lost")]])
    first_loop = _loop(first_client, [])
    coordinator = _coordinator(first_loop, RunStore(tmp_path))
    events = list(first_loop.run("task", coordinator=coordinator))
    assert events[-1].kind == "error"
    assert coordinator.state.status == "failed"
    assert coordinator.state.failure is not None
    assert coordinator.state.failure.code == "internal_error"
