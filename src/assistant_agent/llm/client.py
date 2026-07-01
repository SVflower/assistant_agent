"""模型抽象层：封装 LiteLLM，统一云端 API 与本地后端的调用。

这是整个项目的关键扩展点。业务逻辑只依赖本模块暴露的 LLMClient / LLMResponse，
不感知具体 provider。换后端 = 换传入的 ProviderConfig，本模块代码不变。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from assistant_agent.config.schema import ProviderConfig


class LLMError(Exception):
    """模型调用失败。"""


@dataclass
class ToolCall:
    """模型请求的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """一次模型调用的归一化结果。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """容错解析工具调用参数。

    本地小模型常把 arguments 输出成不规范的 JSON 字符串，甚至空串。
    解析失败时返回空 dict，由上层决定如何处理，不让整个循环崩掉。
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"_raw": parsed}
        except (json.JSONDecodeError, ValueError):
            return {"_raw": raw, "_parse_error": True}
    return {}


def _bypass_proxy_for_local(api_base: str | None) -> None:
    """把本地端点的 host 加进 NO_PROXY，避免系统代理拦截本地请求。

    Windows 的系统级代理会被 httpx（openai/litellm 底层）读取，
    导致发往 127.0.0.1 的本地模型请求被错误地绕进代理而返回 502。
    curl 不读系统代理所以无此问题。这里在调用本地端点前主动豁免。
    """
    if not api_base:
        return
    host = urlparse(api_base).hostname
    if not host:
        return
    for var in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(var, "")
        hosts = {h.strip() for h in existing.split(",") if h.strip()}
        if host not in hosts:
            hosts.add(host)
            os.environ[var] = ",".join(sorted(hosts))


class LLMClient:
    """对 LiteLLM 的薄封装，提供统一的 completion 接口。"""

    def __init__(self, provider: ProviderConfig) -> None:
        self._provider = provider
        _bypass_proxy_for_local(provider.api_base)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """调用模型，返回归一化结果。

        Args:
            messages: OpenAI 格式的消息列表。
            tools: OpenAI 格式的工具 schema 列表；为空则不带工具。

        Raises:
            LLMError: 调用失败或返回结构异常。
        """
        # 延迟导入：litellm 较重，且便于测试时 monkeypatch。
        import litellm

        kwargs: dict[str, Any] = {
            "model": self._provider.model,
            "messages": messages,
            "temperature": self._provider.temperature,
            "max_tokens": self._provider.max_tokens,
        }
        if self._provider.api_key:
            kwargs["api_key"] = self._provider.api_key
        if self._provider.api_base:
            kwargs["api_base"] = self._provider.api_base
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = litellm.completion(**kwargs)
        except Exception as exc:  # litellm 抛出的异常类型繁杂，统一归一
            raise LLMError(f"模型调用失败（{self._provider.model}）：{exc}") from exc

        return self._normalize(response)

    @staticmethod
    def _normalize(response: Any) -> LLMResponse:
        """把 LiteLLM 的返回归一化为 LLMResponse。"""
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError(f"模型返回结构异常：{exc}") from exc

        content = getattr(message, "content", None)

        tool_calls: list[ToolCall] = []
        raw_calls = getattr(message, "tool_calls", None) or []
        for call in raw_calls:
            function = getattr(call, "function", None)
            if function is None:
                continue
            tool_calls.append(
                ToolCall(
                    id=getattr(call, "id", "") or "",
                    name=getattr(function, "name", "") or "",
                    arguments=_parse_arguments(getattr(function, "arguments", None)),
                )
            )

        return LLMResponse(content=content, tool_calls=tool_calls)
