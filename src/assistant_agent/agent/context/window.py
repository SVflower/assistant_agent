"""模型请求的 token 预算估算与硬封套错误。"""

from __future__ import annotations

import json
from typing import Any, Protocol


class ContextWindowError(ValueError):
    """请求无法在配置窗口内构造时抛出，调用方不得继续请求 provider。"""


class TokenEstimator(Protocol):
    """可替换的 token 估算接口；实现失败时应由调用方回退保守估算。"""

    def message_tokens(self, message: dict[str, Any]) -> int: ...

    def tools_tokens(self, schemas: list[dict[str, Any]]) -> int: ...


class ConservativeTokenEstimator:
    """按字符保守估算，适合中英文混合且不依赖具体 provider。"""

    def message_tokens(self, message: dict[str, Any]) -> int:
        content = message.get("content") or ""
        if isinstance(content, dict) and content.get("schema_version") == 1:
            from assistant_agent.contracts.attachments import content_text

            text = content_text(content)
        else:
            text = str(content)
        for call in message.get("tool_calls") or []:
            text += str(call.get("function", {}).get("arguments", ""))
        return len(text) + 4

    def tools_tokens(self, schemas: list[dict[str, Any]]) -> int:
        if not schemas:
            return 0
        return len(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")))


DEFAULT_ESTIMATOR = ConservativeTokenEstimator()


def estimate_message_tokens(
    message: dict[str, Any], estimator: TokenEstimator = DEFAULT_ESTIMATOR
) -> int:
    """估算消息成本；自定义 estimator 失败时回退到保守实现。"""
    try:
        return max(0, int(estimator.message_tokens(message)))
    except Exception:  # noqa: BLE001 - estimator 是扩展点，失败不能破坏硬保证
        return DEFAULT_ESTIMATOR.message_tokens(message)


def estimate_tools_tokens(
    schemas: list[dict[str, Any]], estimator: TokenEstimator = DEFAULT_ESTIMATOR
) -> int:
    """估算工具 schema 成本；自定义 estimator 失败时回退到保守实现。"""
    try:
        return max(0, int(estimator.tools_tokens(schemas)))
    except Exception:  # noqa: BLE001
        return DEFAULT_ESTIMATOR.tools_tokens(schemas)


def truncate_text_to_tokens(text: str, max_tokens: int, overhead: int = 4) -> str:
    """按保守口径裁剪文本，使其连同消息开销不超过 max_tokens。"""
    return text[: max(0, max_tokens - overhead)]
