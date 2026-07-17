"""终端格式化函数测试（token 累计 / 上下文占用等）。"""

from __future__ import annotations

import pytest
from rich.console import Console

from assistant_agent.ui.formatting import (
    build_banner,
    build_response_panel,
    build_turn_status,
    format_context,
    format_elapsed,
    format_usage,
    truncate,
)


@pytest.mark.parametrize("mode", ["readonly", "workspace", "strict", "unrestricted"])
def test_banner_discloses_permission_mode_separately_from_execution(mode):
    console = Console(record=True, width=160)
    console.print(build_banner("provider", "model", "/workspace", mode, "host"))
    output = console.export_text()
    assert mode in output
    assert "无 OS 沙箱" in output
    assert "Assistant Agent" in output
    assert "模型" in output and "位置" in output and "权限" in output
    assert "/workspace" in output
    assert "执行  host" in output
    assert len(output.splitlines()) == 6


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("host", "宿主执行，无 OS 沙箱"),
        ("confined", "workspace  · 路径受限，非 OS 沙箱"),
        ("container", "Shell/Git 容器隔离"),
    ],
)
def test_banner_discloses_actual_execution_boundary(backend, expected):
    console = Console(record=True, width=160)
    console.print(build_banner("provider", "model", "/workspace", "workspace", backend))
    assert expected in console.export_text()


def test_verbose_banner_discloses_full_runtime_details():
    console = Console(record=True, width=160)
    console.print(
        build_banner(
            "cloud",
            "openai/deepseek-v4-pro",
            "/workspace/project",
            "workspace",
            "confined",
            verbose=True,
        )
    )
    output = console.export_text()
    assert "deepseek-v4-pro" in output
    assert "后端  cloud" in output
    assert "/workspace/project" in output


def test_response_panel_has_stable_agent_label_and_horizontal_frame():
    console = Console(record=True, width=80)
    console.print(build_response_panel("**完成**"))
    output = console.export_text()
    assert "$ Assistant" in output and "完成" in output
    assert "**" not in output


def test_turn_status_keeps_usage_context_and_single_line():
    console = Console(record=True, width=72)
    console.print(build_turn_status("openai/model", "3.2s", 1200, 80, 1200, 8000, 72))
    output = console.export_text()
    assert "model" in output
    assert "token ↑1200 ↓80 共 1280" in output
    assert "上下文 1200/8000（15%）" in output
    assert len(output.splitlines()) == 1

    narrow = Console(record=True, width=32)
    narrow.print(build_turn_status("openai/model", "3.2s", 1200, 80, 1200, 8000, 32))
    assert len(narrow.export_text().splitlines()) == 1


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
