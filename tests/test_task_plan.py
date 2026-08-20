"""结构化 TaskPlan 工具、checkpoint 与恢复测试。"""

from __future__ import annotations

import json

from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.persistence.run_store import RunStore
from assistant_agent.providers.ports import ToolCall
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.task_plan import UpdateTaskPlanTool
from tests.support import ToolContextFixture


def _items(active: int = 0) -> list[dict[str, str]]:
    statuses = ["pending", "pending", "pending"]
    statuses[active] = "in_progress"
    return [
        {"item_id": f"step-{index + 1}", "content": content, "status": statuses[index]}
        for index, content in enumerate(("读取输入", "生成成果", "验证成果"))
    ]


def _coordinator(tmp_path) -> RunCoordinator:
    return RunCoordinator.create(
        RunStore(tmp_path / "runs"),
        task="deliver",
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


def _plan_call(coordinator: RunCoordinator, call_id: str, items: list[dict[str, str]]) -> None:
    arguments = {"items": items}
    coordinator.model_completed(
        [
            *coordinator.state.messages,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "update_task_plan",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            },
        ],
        [ToolCall(call_id, "update_task_plan", arguments)],
    )


def test_task_plan_is_revisioned_and_recovered(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(UpdateTaskPlanTool())
    coordinator = _coordinator(tmp_path)
    ctx = ToolContextFixture(workspace_root=tmp_path)
    coordinator.bind_tool_context(ctx)

    first = _items()
    _plan_call(coordinator, "call-1", first)
    result = registry.execute(
        "update_task_plan", {"items": first}, ctx, call_id="call-1", lifecycle=coordinator
    )
    assert result.code == "task_plan_updated"
    assert coordinator.observability_snapshot().task_plan is not None
    assert coordinator.observability_snapshot().task_plan.revision == 1

    coordinator.batch_completed(coordinator.state.messages)
    second = _items(1)
    second[0]["status"] = "completed"
    _plan_call(coordinator, "call-2", second)
    registry.execute(
        "update_task_plan", {"items": second}, ctx, call_id="call-2", lifecycle=coordinator
    )

    recovered = RunCoordinator.load(RunStore(tmp_path / "runs"), "run-1")
    snapshot = recovered.observability_snapshot(persisted=True)
    assert snapshot.task_plan is not None
    assert snapshot.task_plan.revision == 2
    assert [item.status for item in snapshot.task_plan.items] == [
        "completed",
        "in_progress",
        "pending",
    ]
    assert recovered.state.tool_calls[0].replay_policy == "safe_idempotent"


def test_task_plan_rejects_duplicate_ids_and_parallel_active_items(tmp_path) -> None:
    tool = UpdateTaskPlanTool()
    captured = []
    ctx = ToolContextFixture(
        workspace_root=tmp_path,
        task_plan_replace=lambda items: captured.append(items),
    )
    duplicate = _items()
    duplicate[1]["item_id"] = duplicate[0]["item_id"]
    assert tool.run({"items": duplicate}, ctx).code == "task_plan_invalid"

    parallel = _items()
    parallel[1]["status"] = "in_progress"
    assert tool.run({"items": parallel}, ctx).code == "task_plan_invalid"
    assert captured == []
