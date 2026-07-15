"""终端格式化函数测试（token 累计 / 上下文占用等）。"""

from __future__ import annotations

import pytest
from rich.console import Console

from assistant_agent.ui.formatting import (
    build_banner,
    format_context,
    format_elapsed,
    format_usage,
    truncate,
)


@pytest.mark.parametrize("mode", ["readonly", "workspace", "strict", "unrestricted"])
def test_banner_discloses_permission_mode_and_no_os_sandbox(mode):
    console = Console(record=True, width=100)
    console.print(build_banner("provider", "model", "/workspace", mode))
    output = console.export_text()
    assert mode in output
    assert "无 OS 沙箱" in output


def test_format_usage_sums_in_out():
    # 累计视角：↑输入 ↓输出 共=两者之和
    assert format_usage(3000, 200) == "↑3000 ↓200 共 3200"


def test_format_context_percent():
    out = format_context(4000, 8000)
    assert "4000/8000" in out
    assert "50%" in out


def test_format_context_high_usage_colored():
    # ≥80% 用 yellow，≥90% 用 red
    assert "yellow" in format_context(6500, 8000)  # ~81%
    assert "red" in format_context(7300, 8000)  # ~91%


def test_format_context_no_limit():
    # 无预算时只显示占用数，不算百分比
    assert format_context(1234, 0) == "上下文 1234"


def test_truncate():
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdef", 3) == "abc…"


def test_format_elapsed():
    assert format_elapsed(5.2) == "5.2s"
    assert format_elapsed(75) == "1m 15s"
