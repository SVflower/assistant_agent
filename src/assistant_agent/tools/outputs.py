"""受限文本交付物工具。"""

from __future__ import annotations

from typing import Any, Literal, cast

from assistant_agent.contracts.outputs import OutputError
from assistant_agent.contracts.presentation_common import stable_message_id
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.display import ToolDisplay
from assistant_agent.tools.lifecycle import ReplayPolicy
from assistant_agent.tools.models import OutputCaptureIntent, ToolResult
from assistant_agent.tools.permissions import PermissionRequest
from assistant_agent.tools.tool import Tool


def _create_parameters() -> dict[str, Any]:
    return {
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
            "title": {"type": "string", "maxLength": 200},
            "disposition": {"type": "string", "enum": ["inline", "download"]},
        },
        "required": ["filename", "media_type"],
    }


def _bound_output_context(ctx: ToolContext) -> bool:
    return bool(
        ctx.output_store and ctx.current_session_id and ctx.current_run_id and ctx.current_call_id
    )


class _OutputTool(Tool):
    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return []

    def replay_policy(
        self, args: dict[str, Any], ctx: ToolContext, requests: list[PermissionRequest]
    ) -> ReplayPolicy | None:
        return "safe_idempotent"


class CreateOutputTool(_OutputTool):
    name = "create_output"
    description = (
        "声明一个要交付给用户的受管文本文件。只提交文件名、类型和标题；"
        "工具成功后，下一轮只输出完整文件正文，Runtime 会自动流式保存并发布。"
    )

    def __init__(self, max_content_bytes: int = 8192) -> None:
        self.max_content_bytes = max_content_bytes
        self._parameters = _create_parameters()

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay(
            action="生成输出文件", target=str(args.get("filename", ""))[:180], importance="change"
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not _bound_output_context(ctx):
            return ToolResult.error(
                "输出只能在绑定 Session 的 Run 中创建。", code="output_unavailable", executed=False
            )
        output_store = ctx.output_store
        session_id = ctx.current_session_id
        assert output_store is not None and session_id is not None
        try:
            draft_id = output_store.begin_text_draft(
                session_id=session_id,
                run_id=ctx.current_run_id,
                call_id=ctx.current_call_id,
                filename=str(args["filename"]),
                media_type=str(args["media_type"]),
                disposition=str(args.get("disposition", "download")),
                message_id=stable_message_id(ctx.current_run_id),
                title=str(args["title"]) if args.get("title") is not None else None,
            )
        except OutputError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        intent = OutputCaptureIntent(
            draft_id=draft_id,
            session_id=session_id,
            run_id=ctx.current_run_id,
            call_id=ctx.current_call_id,
            filename=str(args["filename"]),
            media_type=str(args["media_type"]),
            disposition=cast(Literal["inline", "download"], args.get("disposition", "download")),
            title=str(args["title"]) if args.get("title") is not None else None,
            max_chunk_bytes=self.max_content_bytes,
        )
        return ToolResult.ok(
            "输出意图已登记。下一轮只输出完整文件正文；不要添加解释、代码围栏或工具调用。",
            code="output_capture_started",
            output_capture=intent,
        )
