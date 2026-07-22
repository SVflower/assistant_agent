"""声明式受控图表展示工具。"""

from __future__ import annotations

from typing import Any

from assistant_agent.contracts.charts import AnyChartArtifact, build_chart_artifact
from assistant_agent.contracts.charts_v2 import build_chart_artifact_v2
from assistant_agent.contracts.events import ToolDisplay
from assistant_agent.tools.chart_input import ChartInputError, normalize_chart_input
from assistant_agent.tools.chart_input_v2 import needs_chart_v2, normalize_chart_v2_input
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.tool import Tool


class PresentChartTool(Tool):
    name = "present_chart"
    description = (
        "受控交互图表；支持常用统计图、多轴和多面板，data_type 可省略。"
        "例：histogram 传原始 rows、value_key 和可选 bin_count。"
        "禁止 option、HTML、URL、formatter、style 和代码。"
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
                "mark": {"enum": ["line", "area", "bar"]},
                "axis": {"enum": ["left", "right"]},
            },
            "required": ["key", "label"],
        }
        nullable_key = {"type": ["string", "null"], "maxLength": 64}
        reference_line = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "axis": {"enum": ["x", "left", "right"]},
                "value": {"type": ["string", "number"]},
                "label": {"type": "string", "maxLength": 128},
            },
            "required": ["axis", "value"],
        }
        reference_band = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "axis": {"enum": ["x", "left", "right"]},
                "start": {"type": ["string", "number"]},
                "end": {"type": ["string", "number"]},
                "label": {"type": "string", "maxLength": 128},
            },
            "required": ["axis", "start", "end"],
        }
        error_bar = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "series_key": {"type": "string", "maxLength": 64},
                "lower_key": {"type": "string", "maxLength": 64},
                "upper_key": {"type": "string", "maxLength": 64},
            },
            "required": ["series_key", "lower_key", "upper_key"],
        }
        annotation = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 200},
                "x_value": {"type": ["string", "number", "null"]},
                "y_value": {"type": ["number", "null"]},
            },
            "required": ["text"],
        }
        chart_types = [
            "line",
            "area",
            "bar",
            "grouped_bar",
            "stacked_bar",
            "percent_stacked_bar",
            "pie",
            "donut",
            "combo_bar_line",
            "dual_axis",
            "scatter",
            "bubble",
            "histogram",
            "boxplot",
            "heatmap",
        ]
        panel = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "panel_title": {"type": ["string", "null"], "maxLength": 160},
                "chart_type": {"enum": chart_types},
                "x_key": nullable_key,
                "y_key": nullable_key,
                "category_key": nullable_key,
                "value_key": nullable_key,
                "group_key": nullable_key,
                "size_key": nullable_key,
                "series": {"type": "array", "maxItems": 8, "items": series},
                "bin_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                "aggregate": {"enum": ["count", "sum", "mean", "min", "max", None]},
                "reference_lines": {"type": "array", "maxItems": 16, "items": reference_line},
                "reference_bands": {"type": "array", "maxItems": 16, "items": reference_band},
                "error_bars": {"type": "array", "maxItems": 16, "items": error_bar},
                "annotations": {"type": "array", "maxItems": 32, "items": annotation},
            },
            "required": ["chart_type"],
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "schema_version": {"enum": [1, 2]},
                "chart_type": {"enum": chart_types},
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
                "y_key": nullable_key,
                "series": {"type": "array", "maxItems": 8, "items": series},
                "category_key": nullable_key,
                "value_key": nullable_key,
                "group_key": nullable_key,
                "size_key": nullable_key,
                "bin_count": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                "aggregate": {"enum": ["count", "sum", "mean", "min", "max", None]},
                "reference_lines": {"type": "array", "maxItems": 16, "items": reference_line},
                "reference_bands": {"type": "array", "maxItems": 16, "items": reference_band},
                "error_bars": {"type": "array", "maxItems": 16, "items": error_bar},
                "annotations": {"type": "array", "maxItems": 32, "items": annotation},
                "panels": {"type": "array", "maxItems": 4, "items": panel},
                "layout": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "columns": {"type": "integer", "minimum": 1, "maximum": 2},
                        "shared_legend": {"type": "boolean"},
                    },
                },
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
        return self._invalid(
            ctx,
            f"{path}: {validator} 校验失败",
            metadata={"field_path": path},
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.current_session_id or not ctx.current_run_id or not ctx.current_call_id:
            return ToolResult.error(
                "图表只能在绑定 Session 的 Run 中创建。",
                code="artifact_rejected",
                executed=False,
            )
        try:
            artifact: AnyChartArtifact
            if needs_chart_v2(args):
                spec_v2 = normalize_chart_v2_input(args)
                artifact = build_chart_artifact_v2(
                    spec_v2,
                    session_id=ctx.current_session_id,
                    run_id=ctx.current_run_id,
                    call_id=ctx.current_call_id,
                )
            else:
                spec_v1 = normalize_chart_input(args)
                artifact = build_chart_artifact(
                    spec_v1,
                    session_id=ctx.current_session_id,
                    run_id=ctx.current_run_id,
                    call_id=ctx.current_call_id,
                )
        except ChartInputError as exc:
            return self._invalid(ctx, str(exc), args=args, metadata=exc.metadata)
        except (TypeError, ValueError):
            return self._invalid(ctx, "图表超过安全存储上限")
        return ToolResult.ok(
            f"已创建图表：{artifact.title}",
            code="chart_presented",
            chart=artifact,
        )

    def _invalid(
        self,
        ctx: ToolContext,
        detail: str,
        *,
        args: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        if args is None:
            previous = ctx.result_count(self.name, "[chart_input_invalid]")
        else:
            identity = _correction_identity(args)
            previous = ctx.result_count_matching(
                self.name,
                "[chart_input_invalid]",
                lambda prior: _correction_identity(prior) == identity,
            )
        retryable = previous == 0
        correction_remaining = 1 if retryable else 0
        action = (
            "请按 field_path 仅修正这一次调用并重新提交。"
            if retryable
            else "修正次数已用完，请停止调用图表工具并继续文字回答。"
        )
        result_metadata = dict(metadata or {})
        result_metadata["correction_remaining"] = correction_remaining
        output = (
            f"[chart_input_invalid] {detail}；correction_remaining={correction_remaining}；{action}"
        )
        return ToolResult.error(
            output,
            code="artifact_rejected",
            retryable=retryable,
            metadata=result_metadata,
            executed=False,
        )


def _correction_identity(args: dict[str, Any]) -> tuple[Any, ...]:
    """忽略数据和可修正字段，只识别同一图表意图。"""
    raw_panels = args.get("panels")
    panels = raw_panels if isinstance(raw_panels, list) and raw_panels else [args]
    mappings: list[tuple[str, ...]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            mappings.append(("invalid",))
            continue
        mappings.append(
            tuple(
                str(panel.get(key) or "")
                for key in (
                    "chart_type",
                    "x_key",
                    "y_key",
                    "category_key",
                    "value_key",
                    "group_key",
                    "size_key",
                )
            )
        )
    return (str(args.get("title") or ""), tuple(mappings))
