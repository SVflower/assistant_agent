"""真实 checkpoint 边界上的 AgentLoop crash/resume 测试。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.recovery import RunCoordinator
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import StreamEvent, ToolCall
from assistant_agent.session.run_store import RunStore
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.registry import ToolRegistry


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
        ToolContext(
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


def test_model_error_can_resume_from_model_pending(tmp_path):
    first_client = ScriptedClient([[StreamEvent(kind="error", text="connection lost")]])
    first_loop = _loop(first_client, [])
    coordinator = _coordinator(first_loop, RunStore(tmp_path))
    events = list(first_loop.run("task", coordinator=coordinator))
    assert events[-1].kind == "error"
    assert coordinator.state.status == "paused"

    resumed_client = ScriptedClient([[StreamEvent(kind="content", text="recovered")]])
    resumed_loop = _loop(resumed_client, [])
    loaded = RunCoordinator.load(RunStore(tmp_path), "run-1")
    events = list(resumed_loop.resume(loaded))
    assert events[-1].text == "recovered"
    assert loaded.state.status == "completed"
