"""受限文本交付物工具。"""

from __future__ import annotations

from typing import Any

from assistant_agent.contracts.outputs import OutputError
from assistant_agent.contracts.presentation_common import stable_message_id
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.display import ToolDisplay
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.tool import Tool


class CreateOutputTool(Tool):
    name = "create_output"
    description = "创建用户可下载的受管 HTML/CSV/JSON/Markdown/文本文件。"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "filename": {"type": "string", "minLength": 1, "maxLength": 180},
            "media_type": {
                "type": "string",
                "enum": [
                    "text/html",
                    "text/markdown",
                    "text/csv",
                    "application/json",
                    "text/plain",
                ],
            },
            "content": {"type": "string"},
            "title": {"type": "string", "maxLength": 200},
            "disposition": {"type": "string", "enum": ["inline", "download"]},
        },
        "required": ["filename", "media_type", "content"],
    }

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return []

    def replay_policy(
        self, args: dict[str, Any], ctx: ToolContext, requests: list[PermissionRequest]
    ) -> ReplayPolicy | None:
        return "safe_idempotent"

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay(
            action="生成输出文件", target=str(args.get("filename", ""))[:180], importance="change"
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if (
            not ctx.output_store
            or not ctx.current_session_id
            or not ctx.current_run_id
            or not ctx.current_call_id
        ):
            return ToolResult.error(
                "输出只能在绑定 Session 的 Run 中创建。", code="output_unavailable", executed=False
            )
        try:
            artifact = ctx.output_store.publish_text(
                session_id=ctx.current_session_id,
                run_id=ctx.current_run_id,
                call_id=ctx.current_call_id,
                filename=str(args["filename"]),
                media_type=str(args["media_type"]),
                content=str(args["content"]),
                disposition=str(args.get("disposition", "download")),
                message_id=stable_message_id(ctx.current_run_id),
                title=str(args["title"]) if args.get("title") is not None else None,
            )
        except OutputError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        return ToolResult.ok(
            f"已创建输出文件：{artifact.filename}（{artifact.size_bytes} bytes）",
            code="output_created",
            output_artifact=artifact,
        )
