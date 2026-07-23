"""模型抽象层：封装 LiteLLM，统一云端 API 与本地后端的调用。

这是整个项目的关键扩展点。业务逻辑依赖 `providers.ports` 中的统一事件，不感知具体 provider。
换后端 = 换传入的 ProviderConfig，本模块代码不变。

LiteLLM 是边缘 adapter，不是业务状态机。第三方异常和形状不同的流式 chunk 必须在这里归一化，
Agent/API 不应 import LiteLLM 类型或解析它的原始异常文本。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

from assistant_agent.config.schema import ProviderConfig
from assistant_agent.providers.content_codec import AttachmentContentCodec
from assistant_agent.providers.ports import (
    ProviderFailure,
    StreamEvent,
    ToolCall,
)

# 让 LiteLLM 用自带的本地价格表，不去 GitHub 拉远程 cost map。
# 我们不用 litellm 的成本计算（token 自己数），联网拉取只会在墙内/弱网首调时超时刷警告。
# 用 setdefault 便于用户仍可用环境变量覆盖。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def classify_provider_exception(exc: BaseException) -> ProviderFailure:
    """按类型和 HTTP 状态分类，不把第三方异常文本带入公共事件。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    name = type(exc).__name__.lower()
    if status == 429:
        return ProviderFailure("provider_rate_limited", "模型服务请求过于频繁，请稍后重试。", True)
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return ProviderFailure("provider_timeout", "模型服务响应超时。", True)
    if (
        isinstance(exc, ConnectionError)
        or "connection" in name
        or (isinstance(status, int) and 500 <= status <= 599)
    ):
        return ProviderFailure("provider_unavailable", "模型服务暂时不可用。", True)
    return ProviderFailure("internal_error", "模型请求配置或执行失败。", False)


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


def _finalize_tool_calls(buffers: dict[int, dict[str, str]]) -> list[ToolCall]:
    """把碎片缓冲区拼接成完整 ToolCall 列表（按 index 顺序）。

    arguments 复用 _parse_arguments 容错解析，坏 JSON 不崩。
    没有 name 的（拼接不完整）跳过。
    """
    result: list[ToolCall] = []
    for index in sorted(buffers):
        buf = buffers[index]
        if not buf["name"]:
            continue
        result.append(
            ToolCall(
                id=buf["id"],
                name=buf["name"],
                arguments=_parse_arguments(buf["args"]),
            )
        )
    return result


def _normalize_usage(usage: Any) -> dict[str, int]:
    """把 litellm 的 usage 对象归一化为简单 dict。"""
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }


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
    """对 LiteLLM 的薄封装，提供统一的 completion 接口。

    流式响应中的文本、reasoning、usage 和 tool arguments 可能分散在多个 chunk；本类负责拼接并
    转换成项目自己的 ``StreamEvent``，上层 Loop 不处理 provider 方言。
    """

    def __init__(
        self,
        provider: ProviderConfig,
        content_codec: AttachmentContentCodec | None = None,
    ) -> None:
        self._provider = provider
        self._content_codec = content_codec
        _bypass_proxy_for_local(provider.api_base)

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """构造 litellm.completion 的公共参数，流式/非流式共用。"""
        kwargs: dict[str, Any] = {
            "model": self._provider.model,
            "messages": (
                self._content_codec.materialize(messages) if self._content_codec else messages
            ),
            "temperature": self._provider.temperature,
            "max_tokens": self._provider.max_tokens,
            "timeout": self._provider.request_timeout,
        }
        if self._provider.api_key:
            kwargs["api_key"] = self._provider.api_key
        if self._provider.api_base:
            kwargs["api_base"] = self._provider.api_base
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamEvent]:
        """流式调用模型，逐步 yield StreamEvent。

        增量顺序大致为：reasoning* → content* → tool_calls?（拼接完成后一次性）→ usage?
        流中途出错时 yield 一个 error 事件（不抛出），此前的增量已经产出、应予保留。

        工具调用在流中是碎片化到达的（首片带 id/name，后续片只带 arguments 片段），
        本方法负责按 index 累积拼接，在流结束后统一产出完整 ToolCall。
        """
        import litellm

        kwargs = self._build_kwargs(messages, tools)
        kwargs["stream"] = True
        # 要求在流末尾附带 token 用量（OpenAI 兼容端点通用参数）
        kwargs["stream_options"] = {"include_usage": True}

        # tool_call 碎片缓冲：index -> {"id","name","args"}
        buffers: dict[int, dict[str, str]] = {}

        try:
            stream = litellm.completion(**kwargs)
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                choice = choices[0] if choices else None
                delta = getattr(choice, "delta", None) if choice else None

                if delta is not None:
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield StreamEvent(kind="reasoning", text=reasoning)

                    content = getattr(delta, "content", None)
                    if content:
                        yield StreamEvent(kind="content", text=content)

                    for frag in getattr(delta, "tool_calls", None) or []:
                        self._accumulate_tool_call(buffers, frag)

                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    yield StreamEvent(kind="usage", usage=_normalize_usage(usage))
        except Exception as exc:  # 流中途失败：产出脱敏 error 事件而非抛出
            failure = classify_provider_exception(exc)
            yield StreamEvent(kind="error", text=failure.safe_message, failure=failure)
            return

        # 流正常结束：把拼接好的工具调用一次性产出
        tool_calls = _finalize_tool_calls(buffers)
        if tool_calls:
            yield StreamEvent(kind="tool_calls", tool_calls=tool_calls)

    @staticmethod
    def _accumulate_tool_call(buffers: dict[int, dict[str, str]], frag: Any) -> None:
        """把一个 tool_call 碎片累加进缓冲区（按 index 分组）。

        首片带 id/name、arguments 为空；后续片 id/name 为 None，只累加 arguments。
        """
        index = getattr(frag, "index", 0) or 0
        buf = buffers.setdefault(index, {"id": "", "name": "", "args": ""})
        frag_id = getattr(frag, "id", None)
        if frag_id:
            buf["id"] = frag_id
        function = getattr(frag, "function", None)
        if function is not None:
            name = getattr(function, "name", None)
            if name:
                buf["name"] = name
            args = getattr(function, "arguments", None)
            if args:
                buf["args"] += args
