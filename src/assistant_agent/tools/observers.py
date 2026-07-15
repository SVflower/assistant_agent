"""Tool 执行前后的同步 observer 接口。"""

from __future__ import annotations

from typing import Any, Protocol

from assistant_agent.tools.permissions import PermissionRequest


class PreToolUseObserver(Protocol):
    def pre_tool_use(
        self, tool: str, args: dict[str, Any], requests: list[PermissionRequest]
    ) -> str | None:
        """返回拒绝原因；None 表示继续。异常由 Registry 按 fail-closed 处理。"""


class PostToolUseObserver(Protocol):
    def post_tool_use(
        self,
        tool: str,
        args: dict[str, Any],
        requests: list[PermissionRequest],
        result: Any,
    ) -> None:
        """执行后只观察，不得替换结果。"""
