"""声明式受控图表展示工具。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from assistant_agent.contracts.charts import ChartSpecV1, build_chart_artifact
from assistant_agent.contracts.events import ToolDisplay
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.tool import Tool


class PresentChartTool(Tool):
    name = "present_chart"
    description = (
        "把已获得的结构化数据展示为受控交互图表。只接受声明式 ChartSpec，"
        "不得传入 ECharts option、HTML、URL、formatter 或执行代码。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        column = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 64},
                "label": {"type": "string", "minLength": 1, "maxLength": 128},
                "data_type": {"enum": ["string", "number", "datetime"]},
                "unit": {"type": ["string", "null"], "maxLength": 128},
            },
            "required": ["key", "label", "data_type"],
        }
        series = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 64},
                "label": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["key", "label"],
        }
        nullable_key = {"type": ["string", "null"], "maxLength": 64}
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "schema_version": {"const": 1},
                "chart_type": {"enum": ["line", "bar", "stacked_bar", "area", "scatter", "donut"]},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "description": {"type": ["string", "null"], "maxLength": 500},
                "source_label": {"type": ["string", "null"], "maxLength": 500},
                "columns": {"type": "array", "minItems": 1, "maxItems": 12, "items": column},
                "rows": {
                    "type": "array",
                    "maxItems": 5000,
                    "items": {
                        "type": "array",
                        "maxItems": 12,
                        "items": {"type": ["string", "number", "null"]},
                    },
                },
                "x_key": nullable_key,
                "series": {"type": "array", "maxItems": 8, "items": series},
                "category_key": nullable_key,
                "value_key": nullable_key,
            },
            "required": ["chart_type", "title", "columns", "rows"],
        }

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return []

    def replay_policy(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
        requests: list[PermissionRequest],
    ) -> ReplayPolicy | None:
        return "safe_idempotent"

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay(
            action="生成图表",
            target=str(args.get("title") or "未命名图表")[:200],
            summary=str(args.get("chart_type") or "chart"),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.current_session_id or not ctx.current_run_id or not ctx.current_call_id:
            return ToolResult.error(
                "图表只能在绑定 Session 的 Run 中创建。",
                code="artifact_rejected",
                executed=False,
            )
        try:
            # JSON mode 保留 strict 标量校验，同时允许 JSON array 映射为不可变 tuple。
            spec = ChartSpecV1.model_validate_json(json.dumps(args, ensure_ascii=False))
            artifact = build_chart_artifact(
                spec,
                session_id=ctx.current_session_id,
                run_id=ctx.current_run_id,
                call_id=ctx.current_call_id,
            )
        except (TypeError, ValueError, ValidationError):
            return ToolResult.error(
                "图表规格无效或超过安全上限，已忽略图表并保留文字回答。",
                code="artifact_rejected",
                retryable=True,
                executed=False,
            )
        return ToolResult.ok(
            f"已创建图表：{artifact.title}",
            code="chart_presented",
            chart=artifact,
        )
