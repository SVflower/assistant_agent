"""MCPTool：把一个 MCP server 工具适配成本地 Tool。

设计要点：
- 命名空间 `mcp__<server>__<tool>`，防跨 server 冲突。
- run() **主动**发起危险确认（registry 不自动确认），category 按 `mcp:<server>:<tool>` 细分——
  否则一次"永久允许"会放行所有 MCP 工具。
- 同步桥：run() 调注入的 caller（manager 提供，内部 run_coroutine_threadsafe）。
- 两条错误通道：caller 抛异常=协议错误→ToolResult.error；结果 isError=True=工具执行错误→回喂模型。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant_agent.tools.base import Tool, ToolContext, ToolResult

#: caller(server, raw_tool, args, timeout) -> CallToolResult 形态对象（有 content/isError），
#: 协议错误应抛异常（含超时 TimeoutError）。
Caller = Callable[[str, str, dict[str, Any], float], Any]

_MAX_DESC = 1024


def extract_content(result: Any) -> tuple[str, bool]:
    """从 CallToolResult 形态对象提取 (文本, is_error)。纯函数，便于单测。

    只拼 text 块；非 text 块给占位。isError 缺省视为 False。
    """
    is_error = bool(getattr(result, "isError", False))
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", None) == "text":
            parts.append(getattr(item, "text", ""))
        else:
            parts.append(f"[非文本内容：{getattr(item, 'type', '未知')}，已省略]")
    text = "\n".join(parts) if parts else "(无内容)"
    return text, is_error


class MCPTool(Tool):
    """一个 MCP server 工具的本地适配器。"""

    def __init__(
        self,
        *,
        server: str,
        registered_name: str,
        raw_tool: str,
        description: str,
        input_schema: dict[str, Any],
        caller: Caller,
        timeout: float,
        auto_approve: bool,
    ) -> None:
        self._server = server
        self.name = registered_name
        self._raw_tool = raw_tool
        self.description = (description or "")[:_MAX_DESC]
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._caller = caller
        self._timeout = timeout
        self._auto_approve = auto_approve

    @property
    def parameters(self) -> dict[str, Any]:
        return self._input_schema

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 权限：MCP 工具默认都要确认，category 按 server+tool 细分（不共用，防一次放行全部）。
        if not self._auto_approve:
            category = f"mcp:{self._server}:{self._raw_tool}"
            message = f"允许调用外部 MCP 工具 {self.name}？（server={self._server}）"
            if not ctx.request_confirm(category, message):
                return ToolResult.error(f"用户拒绝调用 MCP 工具 {self.name}")
        # 同步桥调用；协议错误（含超时）由 caller 抛出，统一转 error。
        try:
            result = self._caller(self._server, self._raw_tool, args, self._timeout)
        except TimeoutError:
            return ToolResult.error(f"MCP 工具 {self.name} 调用超时（>{self._timeout}s）")
        except Exception as exc:  # 协议/连接错误 = 我方通道
            return ToolResult.error(f"MCP 工具 {self.name} 调用失败：{exc}")
        text, is_error = extract_content(result)
        # 工具执行错误：isError=True，文本回喂模型让它换做法。
        return ToolResult(output=text, is_error=is_error)
