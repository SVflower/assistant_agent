"""系统提示词测试。"""

from __future__ import annotations

import platform

from assistant_agent.agent.prompts import SYSTEM_PROMPT, build_system_prompt


def test_build_system_prompt_includes_base():
    prompt = build_system_prompt()
    assert SYSTEM_PROMPT in prompt


def test_system_prompt_names_all_tools():
    # 显式列出真实工具名，帮小模型对齐 function-calling schema
    for tool_name in ("read_file", "write_file", "list_dir", "run_shell"):
        assert tool_name in SYSTEM_PROMPT


def test_system_prompt_has_fewshot_example():
    # 含一段演示正确工具循环节奏的 few-shot 示例
    assert "示例" in SYSTEM_PROMPT


def test_build_system_prompt_includes_os():
    prompt = build_system_prompt()
    assert platform.system() in prompt
    assert "当前运行环境" in prompt


def test_build_system_prompt_includes_date():
    prompt = build_system_prompt()
    # 含当前日期时间说明，让模型能回答"今天几号/星期几"
    assert "当前日期时间" in prompt
    assert any(w in prompt for w in ["周一", "周二", "周三", "周四", "周五", "周六", "周日"])


def test_prompt_encodes_two_layers():
    """提示词区分层1澄清与层2权限确认。"""
    # 层1：需求澄清（意图）
    assert "需求澄清" in SYSTEM_PROMPT
    # 层2：危险操作交给系统确认，不自然语言问
    assert "危险操作" in SYSTEM_PROMPT
    assert "不要用自然语言问" in SYSTEM_PROMPT
    assert "不受 OS 沙箱隔离" in SYSTEM_PROMPT
    assert "由系统自动拦截" not in SYSTEM_PROMPT


def test_interactive_mode_allows_clarification():
    prompt = build_system_prompt(interactive=True)
    assert "交互模式" in prompt


def test_run_mode_assumes_instead_of_asking():
    prompt = build_system_prompt(interactive=False)
    assert "单次任务模式" in prompt
    # run 模式应指示"按假设执行"而非提问
    assert "假设" in prompt
