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
