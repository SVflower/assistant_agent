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


def _create_parameters(max_content_chars: int) -> dict[str, Any]:
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
            "content": {"type": "string", "maxLength": max_content_chars},
            "title": {"type": "string", "maxLength": 200},
            "disposition": {"type": "string", "enum": ["inline", "download"]},
        },
        "required": ["filename", "media_type", "content"],
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
        "创建短小的受管文本文件。content 较长（尤其 HTML）时不要调用；"
        "改用 manage_output 的 begin、append、finalize 动作。"
    )

    def __init__(self, max_content_bytes: int = 8192) -> None:
        self.max_content_bytes = max_content_bytes
        self._parameters = _create_parameters(max_content_bytes)

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
        if len(str(args["content"]).encode("utf-8")) > self.max_content_bytes:
            return ToolResult.error(
                f"短输出超过 {self.max_content_bytes} UTF-8 bytes；请改用分段输出工具。",
                code="output_limit_exceeded",
                executed=False,
            )
        output_store = ctx.output_store
        session_id = ctx.current_session_id
        assert output_store is not None and session_id is not None
        try:
            artifact = output_store.publish_text(
                session_id=session_id,
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


class BeginOutputTool(_OutputTool):
    name = "begin_output"
    description = "开始一个长文本受管输出草稿，返回 draft_id；之后按序追加小块。"
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
            "title": {"type": "string", "maxLength": 200},
            "disposition": {"type": "string", "enum": ["inline", "download"]},
        },
        "required": ["filename", "media_type"],
    }

    def __init__(self, max_chunk_bytes: int = 8192) -> None:
        self.max_chunk_bytes = max_chunk_bytes

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay(
            action="准备输出文件", target=str(args.get("filename", ""))[:180], importance="change"
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not _bound_output_context(ctx):
            return ToolResult.error(
                "输出只能在绑定 Session 的 Run 中创建。",
                code="output_unavailable",
                executed=False,
            )
        assert ctx.output_store and ctx.current_session_id
        try:
            draft_id = ctx.output_store.begin_text_draft(
                session_id=ctx.current_session_id,
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
        return ToolResult.ok(
            f"[output_draft_started] draft_id={draft_id}。"
            f"请从 chunk_index=0 开始调用 manage_output(action=append)，"
            f"每块最多 {self.max_chunk_bytes} UTF-8 bytes。",
            code="output_draft_started",
            metadata={
                "draft_id": draft_id,
                "next_chunk_index": 0,
                "max_chunk_bytes": self.max_chunk_bytes,
            },
        )


class AppendOutputTool(_OutputTool):
    name = "append_output"
    description = "按顺序向受管输出草稿追加一小块 UTF-8 文本；每块最多 8192 bytes。"

    def __init__(self, max_chunk_bytes: int = 8192, max_draft_chunks: int = 256) -> None:
        self._parameters = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "pattern": "^draft_[a-f0-9]{32}$"},
                "chunk_index": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": max_draft_chunks - 1,
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": max_chunk_bytes,
                },
            },
            "required": ["draft_id", "chunk_index", "content"],
        }

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay(
            action="写入输出分块",
            target=f"第 {args.get('chunk_index', '?')} 块",
            importance="change",
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not _bound_output_context(ctx):
            return ToolResult.error(
                "输出只能在绑定 Session 的 Run 中创建。",
                code="output_unavailable",
                executed=False,
            )
        assert ctx.output_store and ctx.current_session_id
        try:
            size = ctx.output_store.append_text_draft(
                session_id=ctx.current_session_id,
                run_id=ctx.current_run_id,
                draft_id=str(args["draft_id"]),
                chunk_index=int(args["chunk_index"]),
                content=str(args["content"]),
            )
        except OutputError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        next_index = int(args["chunk_index"]) + 1
        return ToolResult.ok(
            f"[output_chunk_appended] 已写入第 {args['chunk_index']} 块，累计 {size} bytes；"
            f"下一块 chunk_index={next_index}。",
            code="output_chunk_appended",
            metadata={"next_chunk_index": next_index, "size_bytes": size},
        )


class FinalizeOutputTool(_OutputTool):
    name = "finalize_output"
    description = "完成受管输出草稿；原子发布最终文件并返回 OutputArtifact。"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"draft_id": {"type": "string", "pattern": "^draft_[a-f0-9]{32}$"}},
        "required": ["draft_id"],
    }

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay(action="完成输出文件", target="受管草稿", importance="change")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not _bound_output_context(ctx):
            return ToolResult.error(
                "输出只能在绑定 Session 的 Run 中创建。",
                code="output_unavailable",
                executed=False,
            )
        assert ctx.output_store and ctx.current_session_id
        try:
            artifact = ctx.output_store.finalize_text_draft(
                session_id=ctx.current_session_id,
                run_id=ctx.current_run_id,
                draft_id=str(args["draft_id"]),
            )
        except OutputError as exc:
            return ToolResult.error(str(exc), code=exc.code, executed=False)
        return ToolResult.ok(
            f"已创建输出文件：{artifact.filename}（{artifact.size_bytes} bytes）",
            code="output_created",
            output_artifact=artifact,
        )


class ManageOutputTool(_OutputTool):
    name = "manage_output"
    description = (
        "分段创建长文本交付文件。先 action=begin；再按 chunk_index 从0递增执行 append，"
        "每块保持较小；最后 action=finalize。"
    )

    def __init__(self, max_chunk_bytes: int = 8192, max_draft_chunks: int = 256) -> None:
        self.max_chunk_bytes = max_chunk_bytes
        self.max_draft_chunks = max_draft_chunks
        self._parameters = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["begin", "append", "finalize"]},
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
                "draft_id": {"type": "string", "pattern": "^draft_[a-f0-9]{32}$"},
                "chunk_index": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": max_draft_chunks - 1,
                },
                "content": {"type": "string", "minLength": 1, "maxLength": max_chunk_bytes},
            },
            "required": ["action"],
        }

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        labels = {"begin": "准备输出文件", "append": "写入输出分块", "finalize": "完成输出文件"}
        return ToolDisplay(
            action=labels.get(str(args.get("action")), "管理输出文件"),
            target=str(args.get("filename") or args.get("draft_id") or "")[:180],
            importance="change",
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action", ""))
        if action == "begin":
            required = {"filename", "media_type"}
            tool: Tool = BeginOutputTool(self.max_chunk_bytes)
        elif action == "append":
            required = {"draft_id", "chunk_index", "content"}
            tool = AppendOutputTool(self.max_chunk_bytes, self.max_draft_chunks)
        elif action == "finalize":
            required = {"draft_id"}
            tool = FinalizeOutputTool()
        else:
            return ToolResult.error("未知输出动作", code="output_invalid", executed=False)
        missing = sorted(required - args.keys())
        if missing:
            return ToolResult.error(
                f"缺少 {action} 参数：{', '.join(missing)}",
                code="output_invalid",
                retryable=True,
                executed=False,
            )
        delegated = {key: value for key, value in args.items() if key != "action"}
        return tool.run(delegated, ctx)
