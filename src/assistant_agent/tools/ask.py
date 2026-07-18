"""ask_user 工具：层1 意图澄清。

当需求有歧义或有多个合理方案需用户定夺时，模型调用本工具向用户提问、列出选项，
用户选择作为工具结果喂回，循环在同一轮内继续。

与层2 权限确认（confirm）区分：本工具只澄清意图、无副作用、不算危险操作。
非交互环境（管道/无 tty）自动退化，不阻塞自动化。
"""

from __future__ import annotations

from typing import Any

from assistant_agent.tools.context import NO_USER_AVAILABLE, ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.tool import Tool


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "当需求有歧义、或有多个合理方案需用户定夺时，向用户提问并列出候选选项，"
        "等用户选择后再继续。这是澄清意图，不是执行授权；不要用它来确认危险操作。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2~5 个候选选项，供用户选择",
                },
            },
            "required": ["question", "options"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = args.get("question")
        if not question:
            return ToolResult.error("缺少参数 question")
        options = args.get("options")
        if not isinstance(options, list) or not options:
            return ToolResult.error("缺少参数 options（需至少一个候选选项）")

        # 可交互性由 Runtime/InteractionPort 决定，公共服务不依赖进程 TTY。
        if not ctx.interactive:
            return ToolResult.ok(NO_USER_AVAILABLE)

        choice = ctx.request_question(question, [str(o) for o in options])
        return ToolResult.ok(f"用户选择：{choice}")

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return [
            PermissionRequest(
                self.name,
                Capability.USER_INTERACTION,
                "current_user",
                "向当前用户请求需求澄清",
            )
        ]
