"""模型显式维护当前 Run 的结构化任务计划。"""

from __future__ import annotations

from typing import Any

from assistant_agent.contracts.observability import TaskPlanItem
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.tool import Tool


class UpdateTaskPlanTool(Tool):
    name = "update_task_plan"
    description = (
        "记录并更新当前任务的完整执行清单。仅用于需要多个具体步骤的任务；简单问答不要调用。"
        "每次必须提交完整列表，调用会替换上一版。开始一项时标记 in_progress，完成后立即标记 "
        "completed；顺序执行时最多一项 in_progress。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "description": "完整任务列表；不是增量更新。",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "item_id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 100,
                                "description": "本次任务内稳定且唯一的短 ID。",
                            },
                            "content": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                                "description": "简短、可验收的动作描述。",
                            },
                            "status": {
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["item_id", "content", "status"],
                    },
                }
            },
            "required": ["items"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_items = args.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return ToolResult.error(
                "任务列表不能为空。",
                code="task_plan_invalid",
                retryable=True,
                executed=False,
            )
        try:
            items = tuple(TaskPlanItem.model_validate(item, strict=True) for item in raw_items)
        except (TypeError, ValueError) as exc:
            return ToolResult.error(
                f"任务列表无效：{exc}",
                code="task_plan_invalid",
                retryable=True,
                executed=False,
            )
        if len({item.item_id for item in items}) != len(items):
            return ToolResult.error(
                "任务 item_id 必须唯一。",
                code="task_plan_invalid",
                retryable=True,
                executed=False,
            )
        if sum(item.status == "in_progress" for item in items) > 1:
            return ToolResult.error(
                "顺序任务最多只能有一项 in_progress。",
                code="task_plan_invalid",
                retryable=True,
                executed=False,
            )
        snapshot = ctx.replace_task_plan(items)
        if snapshot is None:
            return ToolResult.error(
                "当前运行入口不支持任务计划。",
                code="task_plan_unavailable",
                executed=False,
            )
        counts = {
            status: sum(item.status == status for item in items)
            for status in ("pending", "in_progress", "completed")
        }
        return ToolResult.ok(
            "任务计划已更新："
            f"{counts['pending']} 项待处理，{counts['in_progress']} 项进行中，"
            f"{counts['completed']} 项已完成。",
            code="task_plan_updated",
            metadata={"revision": snapshot.revision, **counts},
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        del args, ctx
        return []

    def replay_policy(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
        requests: list[PermissionRequest],
    ) -> ReplayPolicy | None:
        del args, ctx, requests
        return "safe_idempotent"


__all__ = ["UpdateTaskPlanTool"]
