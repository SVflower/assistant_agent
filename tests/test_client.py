"""LLMClient 流式碎片拼接等测试（还 D1 债：M2 最脆弱逻辑此前无直接测试）。"""

from __future__ import annotations

import os
from types import SimpleNamespace

from assistant_agent.config.schema import ProviderConfig
from assistant_agent.llm.client import (
    LLMClient,
    _bypass_proxy_for_local,
    _finalize_tool_calls,
    _normalize_usage,
    _parse_arguments,
    classify_provider_exception,
)


class _HTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_provider_exception_classification_is_stable_and_safe():
    assert classify_provider_exception(_HTTPError(429)).code == "provider_rate_limited"
    assert classify_provider_exception(_HTTPError(503)).code == "provider_unavailable"
    assert classify_provider_exception(TimeoutError("secret-token")).code == "provider_timeout"
    failure = classify_provider_exception(RuntimeError("api_key=secret-token"))
    assert failure.code == "internal_error"
    assert "secret-token" not in failure.safe_message


def _frag(index=0, id=None, name=None, arguments=None):
    """模拟流式 chunk 里 delta.tool_calls[i] 的碎片对象。"""
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_tool_call_fragment_assembly():
    """复现实测碎片格式：首片带 id+name(args空)，后续片只带 args 片段。"""
    buffers: dict[int, dict[str, str]] = {}
    # 首片
    LLMClient._accumulate_tool_call(
        buffers, _frag(0, id="call_1", name="get_weather", arguments="")
    )
    # 后续片逐字拼 arguments：{"city":"北京"}
    for piece in ['{"', "city", '":"', "北京", '"}']:
        LLMClient._accumulate_tool_call(buffers, _frag(0, arguments=piece))

    calls = _finalize_tool_calls(buffers)
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "北京"}


def test_multiple_tool_calls_by_index():
    """一轮多个工具调用按 index 分组，拼出多个 ToolCall。"""
    buffers: dict[int, dict[str, str]] = {}
    LLMClient._accumulate_tool_call(buffers, _frag(0, id="c0", name="f0", arguments='{"a":1}'))
    LLMClient._accumulate_tool_call(buffers, _frag(1, id="c1", name="f1", arguments='{"b":2}'))
    calls = _finalize_tool_calls(buffers)
    assert [c.name for c in calls] == ["f0", "f1"]
    assert calls[0].arguments == {"a": 1}
    assert calls[1].arguments == {"b": 2}


def test_bad_json_args_do_not_crash():
    """碎片拼出坏 JSON（本地小模型高发）→ 容错，不崩。"""
    buffers: dict[int, dict[str, str]] = {}
    LLMClient._accumulate_tool_call(buffers, _frag(0, id="c", name="f", arguments="{bad json"))
    calls = _finalize_tool_calls(buffers)
    assert len(calls) == 1
    # 坏 JSON 被 _parse_arguments 兜底，标记 _parse_error 而非抛异常
    assert calls[0].arguments.get("_parse_error") is True


def test_finalize_skips_nameless_fragments():
    """拼接不完整（无 name）的调用被跳过，不产出半个 ToolCall。"""
    buffers = {0: {"id": "x", "name": "", "args": "{}"}}
    assert _finalize_tool_calls(buffers) == []


def test_parse_arguments_cases():
    assert _parse_arguments('{"k": 1}') == {"k": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments({"already": "dict"}) == {"already": "dict"}
    assert _parse_arguments("not json")["_parse_error"] is True


def test_normalize_usage():
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assert _normalize_usage(usage) == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_bypass_proxy_adds_local_host(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _bypass_proxy_for_local("http://127.0.0.1:1234/v1")
    assert "127.0.0.1" in os.environ.get("NO_PROXY", "")


def test_bypass_proxy_ignores_none():
    # 云端（无 api_base）不应改动环境
    before = os.environ.get("NO_PROXY", "")
    _bypass_proxy_for_local(None)
    assert os.environ.get("NO_PROXY", "") == before


def test_provider_request_timeout_is_forwarded():
    client = LLMClient(ProviderConfig(model="openai/fake", request_timeout=7))
    assert client._build_kwargs([], None)["timeout"] == 7


def test_stream_provider_error_does_not_expose_original_exception(monkeypatch):
    import litellm

    def fail(**_kwargs):
        raise _HTTPError(429)

    monkeypatch.setattr(litellm, "completion", fail)
    events = list(LLMClient(ProviderConfig(model="openai/fake")).complete_stream([]))

    assert len(events) == 1
    assert events[0].failure is not None
    assert events[0].failure.code == "provider_rate_limited"
    assert "429" not in events[0].text
