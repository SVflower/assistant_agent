"""声明式受控图表展示工具。"""

from __future__ import annotations

from typing import Any

from assistant_agent.contracts.charts import build_chart_artifact
from assistant_agent.contracts.events import ToolDisplay
from assistant_agent.tools.chart_input import ChartInputError, normalize_chart_input
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.tool import Tool


class PresentChartTool(Tool):
    name = "present_chart"
    description = (
        "受控交互图表；data_type 可省略并安全推断。禁止 option、HTML、URL、formatter、代码。"
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
            "required": ["key", "label"],
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

    def argument_validation_error(
        self,
        message: str,
        metadata: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        path = ".".join(str(part) for part in metadata.get("path", [])) or "$"
        validator = str(metadata.get("validator", "invalid"))
        return self._invalid(ctx, f"{path}: {validator} 校验失败")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.current_session_id or not ctx.current_run_id or not ctx.current_call_id:
            return ToolResult.error(
                "图表只能在绑定 Session 的 Run 中创建。",
                code="artifact_rejected",
                executed=False,
            )
        try:
            spec = normalize_chart_input(args)
            artifact = build_chart_artifact(
                spec,
                session_id=ctx.current_session_id,
                run_id=ctx.current_run_id,
                call_id=ctx.current_call_id,
            )
        except ChartInputError as exc:
            return self._invalid(ctx, str(exc))
        except (TypeError, ValueError):
            return self._invalid(ctx, "图表超过安全存储上限")
        return ToolResult.ok(
            f"已创建图表：{artifact.title}",
            code="chart_presented",
            chart=artifact,
        )

    def _invalid(self, ctx: ToolContext, detail: str) -> ToolResult:
        previous = ctx.result_count(self.name, "[chart_input_invalid]")
        retryable = previous == 0
        action = (
            "请仅修正这一次调用。"
            if retryable
            else "修正次数已用完，请停止调用图表工具并继续文字回答。"
        )
        return ToolResult.error(
            f"[chart_input_invalid] {detail}；{action}",
            code="artifact_rejected",
            retryable=retryable,
            executed=False,
        )
