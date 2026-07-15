"""结构化 Web 搜索与网页读取工具。"""

from __future__ import annotations

from typing import Any

from assistant_agent.tools.base import Tool, ToolContext
from assistant_agent.tools.display import ToolDisplay, safe_text
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.result import ToolResult
from assistant_agent.web.client import WebClient, WebError, hostname_for_url


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "搜索实时公开网页，返回标题、URL、摘要、来源和查询时间。涉及当前事件或未知事实时使用；"
        "搜索结果不是已验证事实，关键结论应再 fetch_url 阅读来源。"
    )

    def __init__(self, client: WebClient) -> None:
        self._client = client

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "搜索查询"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self._client.config.search.max_results,
                    "description": "返回结果上限",
                },
                "freshness": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "可选时间范围",
                },
            },
            "required": ["query"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            rows, searched_at = self._client.search(
                str(args["query"]),
                max_results=args.get("max_results"),
                freshness=args.get("freshness"),
            )
        except WebError as exc:
            return ToolResult.error(str(exc), code=exc.code, retryable=exc.retryable)
        if not rows:
            return ToolResult.ok(
                "未找到搜索结果",
                metadata={
                    "result_count": 0,
                    "searched_at": searched_at,
                    "backend": self._client.backend.name,
                },
            )
        parts: list[str] = []
        for index, row in enumerate(rows, start=1):
            parts.extend(
                [
                    f"[{index}] {row.title}",
                    f"URL: {row.url}",
                    f"摘要: {row.snippet or '(无摘要)'}",
                ]
            )
        parts.append(f"查询时间: {searched_at} · backend: {self._client.backend.name}")
        return ToolResult.ok(
            "\n".join(parts),
            metadata={
                "result_count": len(rows),
                "searched_at": searched_at,
                "backend": self._client.backend.name,
                "results": [row.to_dict() for row in rows],
                "source_urls": [row.url for row in rows],
            },
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        return [
            PermissionRequest(
                self.name,
                Capability.NETWORK_ACCESS,
                self._client.search_target,
                "搜索词将发送给配置的公开搜索服务",
            )
        ]

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay("搜索网络", safe_text(args.get("query", ""), 120))

    def display_result(self, args: dict[str, Any], result: ToolResult) -> ToolDisplay:
        call = self.display_call(args)
        if result.is_error:
            return ToolDisplay(
                call.action, call.target, safe_text(result.output, 180), result.output
            )
        count = result.metadata.get("result_count", 0)
        return ToolDisplay(
            call.action,
            call.target,
            f"找到 {count} 个来源",
            safe_text(result.output, 1000, multiline=True),
        )


class FetchURLTool(Tool):
    name = "fetch_url"
    description = (
        "读取公开 HTTP(S) URL 的有界正文，返回最终 URL、标题、内容类型、抓取时间和截断状态。"
        "不支持登录、脚本渲染、二进制文件、localhost 或私网地址。"
    )

    def __init__(self, client: WebClient) -> None:
        self._client = client

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1, "description": "要读取的公开 URL"}
            },
            "required": ["url"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            page = self._client.fetch(str(args["url"]))
        except WebError as exc:
            return ToolResult.error(str(exc), code=exc.code, retryable=exc.retryable)
        header = [f"来源: {page.url}", f"抓取时间: {page.fetched_at}"]
        if page.title:
            header.append(f"标题: {page.title}")
        header.append(f"内容类型: {page.content_type}")
        header.append("")
        return ToolResult.ok(
            "\n".join(header) + page.content,
            metadata={
                "url": page.url,
                "title": page.title,
                "content_type": page.content_type,
                "fetched_at": page.fetched_at,
                "truncated": page.truncated,
                "bytes_read": page.bytes_read,
                "content_chars": len(page.content),
                "source_urls": [page.url],
            },
        )

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        url = str(args.get("url", ""))
        return [
            PermissionRequest(
                self.name,
                Capability.NETWORK_ACCESS,
                hostname_for_url(url),
                "将连接外部站点并把公开页面正文返回给模型",
            )
        ]

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        return ToolDisplay("读取网页", safe_text(args.get("url", ""), 140))

    def display_result(self, args: dict[str, Any], result: ToolResult) -> ToolDisplay:
        call = self.display_call(args)
        if result.is_error:
            return ToolDisplay(
                call.action, call.target, safe_text(result.output, 180), result.output
            )
        suffix = "（已截断）" if result.metadata.get("truncated") else ""
        summary = f"读取 {result.metadata.get('content_chars', 0)} 字符{suffix}"
        return ToolDisplay(
            call.action, call.target, summary, safe_text(result.output, 1000, multiline=True)
        )
