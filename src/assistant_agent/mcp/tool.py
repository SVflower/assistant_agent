"""MCPTool：把一个 MCP server 工具适配成本地 Tool。

设计要点：
- 命名空间 `mcp__<server>__<tool>`，防跨 server 冲突。
- run() **主动**发起危险确认（registry 不自动确认），category 按 `mcp:<server>:<tool>` 细分——
  否则一次"永久允许"会放行所有 MCP 工具。
- 同步桥：run() 调注入的 caller（manager 提供，内部 run_coroutine_threadsafe）。
- 两条错误通道：caller 抛异常=协议错误→ToolResult.error；结果 isError=True=工具执行错误→回喂模型。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from assistant_agent.obs import sanitize_for_display
from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest

#: caller(server, raw_tool, args, timeout) -> CallToolResult 形态对象（有 content/isError），
#: 协议错误应抛异常（含超时 TimeoutError）。
Caller = Callable[[str, str, dict[str, Any], float], Any]

_MAX_DESC = 1024


def extract_result(result: Any) -> tuple[str, bool, Any | None]:
    """提取模型文本、错误标志和 structuredContent。纯函数，便于单测。

    只拼 text 块；非 text 块给占位。isError 缺省视为 False。
    """
    is_error = bool(getattr(result, "isError", False))
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", None) == "text":
            parts.append(getattr(item, "text", ""))
        else:
            parts.append(f"[非文本内容：{getattr(item, 'type', '未知')}，已省略]")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        structured_text = json.dumps(structured, ensure_ascii=False, sort_keys=True, default=str)
        parts.append(f"structuredContent:\n{structured_text}")
    text = "\n".join(parts) if parts else "(无内容)"
    return text, is_error, structured


def extract_content(result: Any) -> tuple[str, bool]:
    """兼容入口：返回旧的 (文本, is_error) 二元组。"""
    text, is_error, _structured = extract_result(result)
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
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self._server = server
        self.name = registered_name
        self._raw_tool = raw_tool
        self.description = (description or "")[:_MAX_DESC]
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._caller = caller
        self._timeout = timeout
        self._auto_approve = auto_approve
        self._output_schema = output_schema

    @property
    def parameters(self) -> dict[str, Any]:
        return self._input_schema

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 同步桥调用；协议错误（含超时）由 caller 抛出，统一转 error。
        try:
            result = self._caller(self._server, self._raw_tool, args, self._timeout)
        except TimeoutError:
            return ToolResult.error(
                f"MCP 工具 {self.name} 调用超时（>{self._timeout}s）",
                code="timeout",
                retryable=True,
            )
        except Exception as exc:  # 协议/连接错误 = 我方通道
            return ToolResult.error(
                f"MCP 工具 {self.name} 调用失败：{exc}",
                code="mcp_transport_error",
                retryable=True,
            )
        text, is_error, structured = extract_result(result)
        metadata: dict[str, Any] = {}
        if structured is not None:
            metadata["structured_content"] = structured
        if self._output_schema is not None:
            payload = json.dumps(
                self._output_schema, ensure_ascii=False, sort_keys=True, default=str
            )
            metadata["output_schema_hash"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
        # 工具执行错误：isError=True，文本回喂模型让它换做法。
        if is_error:
            return ToolResult.error(text, code="mcp_tool_error", retryable=True, metadata=metadata)
        return ToolResult.ok(text, metadata=metadata)

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        safe_args = json.dumps(
            sanitize_for_display(args), ensure_ascii=False, sort_keys=True, default=str
        )
        if len(safe_args) > 1000:
            safe_args = safe_args[:1000] + f"…(+{len(safe_args) - 1000} chars)"
        target = f"{self._server}/{self._raw_tool} args={safe_args}"
        common = {"trusted_server": self._auto_approve, "args": args}
        requests = [
            PermissionRequest(
                self.name,
                Capability.MCP_CALL,
                target,
                "外部 MCP server 可能产生副作用；server 元数据默认不可信",
                metadata=common,
            )
        ]
        if not self._auto_approve:
            requests.append(
                PermissionRequest(
                    self.name,
                    Capability.NETWORK_ACCESS,
                    self._server,
                    "未信任 MCP server 可能访问开放网络或外部系统",
                    metadata=common,
                )
            )
        return requests
