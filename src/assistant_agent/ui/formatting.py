"""终端展示的纯格式化辅助函数（无状态，从 console.py 抽出以保持模块聚焦）。"""

from __future__ import annotations

import json
from typing import Any


def format_args(args: dict[str, Any] | None) -> str:
    """把工具调用参数格式化为一行 k=v（长值截断）。"""
    if not args:
        return ""
    try:
        items = []
        for k, v in args.items():
            v_str = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            items.append(f"{k}={truncate(v_str, 80)}")
        return ", ".join(items)
    except (TypeError, ValueError):
        return str(args)


def truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def format_usage(usage: dict[str, int]) -> str:
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens", 0)
    return f"↑{prompt} ↓{completion} 共 {total}"


def format_elapsed(seconds: float) -> str:
    """人类可读的耗时：<60s 显示秒，否则分秒。"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"
