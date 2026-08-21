"""系统提示词测试。"""

from __future__ import annotations

import platform

from assistant_agent.agent.prompts import SYSTEM_PROMPT, build_system_prompt


def test_build_system_prompt_includes_base():
    prompt = build_system_prompt()
    assert SYSTEM_PROMPT in prompt


def test_chart_prompt_tracks_runtime_capability():
    assert "present_chart" in build_system_prompt(chart_presentation=True)
    assert "present_chart" not in build_system_prompt(chart_presentation=False)


def test_web_prompt_proactively_uses_mermaid_for_logic_diagrams():
    prompt = build_system_prompt(runtime_profile="web")

    assert "主动在回答中输出标准" in prompt
    assert "`mermaid` fenced code" in prompt
    assert "sequenceDiagram" in prompt
    assert "flowchart" in prompt
    assert "stateDiagram-v2" in prompt
    assert "数值分析仍使用" in prompt
    assert "不输出 click、HTML、JavaScript、外部 URL" in prompt


def test_cli_prompt_does_not_assume_interactive_mermaid_renderer():
    assert "`mermaid` fenced code" not in build_system_prompt(runtime_profile="cli")


def test_web_task_plan_prompt_still_tracks_runtime_capability():
    enabled = build_system_prompt(runtime_profile="web", task_planning=True)
    disabled = build_system_prompt(runtime_profile="web", task_planning=False)

    assert "update_task_plan" in enabled
    assert "update_task_plan" not in disabled


def test_chart_prompt_directs_one_semantic_correction_without_guessing_aggregate():
    prompt = build_system_prompt(chart_presentation=True)
    assert "按 field_path 重调一次" in prompt
    assert "panels[i]" in prompt
    assert "聚合语义不猜" in prompt


def test_chart_prompt_preserves_axis_semantics_and_units():
    prompt = build_system_prompt(chart_presentation=True)
    assert "columns[].unit" in prompt
    assert "ISO 时间列用 datetime" in prompt
    assert "批次/子组/" in prompt
    assert "预格式化区间用 string" in prompt
    assert "UCL/LCL/CL" in prompt
    assert "轴单位" in prompt
    assert "demo_data" in prompt
    assert "20000 cells" in prompt
    assert "5000 行乘 12 列" in prompt

    disabled = build_system_prompt(chart_presentation=False)
    assert "columns[].unit" not in disabled
    assert "UCL/LCL/CL" not in disabled
    assert "demo_data" not in disabled
    assert "20000 cells" not in disabled


def test_system_prompt_names_all_tools():
    # 显式列出真实工具名，帮小模型对齐 function-calling schema
    for tool_name in ("read_file", "write_file", "list_dir", "run_shell"):
        assert tool_name in SYSTEM_PROMPT
    assert "inspect_runtime()" in SYSTEM_PROMPT
    assert "能力自省必须用它" in SYSTEM_PROMPT


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


def test_runtime_inspection_prompt_matches_registration():
    assert "inspect_runtime()" in build_system_prompt(runtime_inspection=True)
    assert "inspect_runtime()" not in build_system_prompt(runtime_inspection=False)


def test_managed_process_prompt_matches_registration():
    enabled = build_system_prompt(managed_process=True)
    disabled = build_system_prompt(managed_process=False)
    assert "manage_process(action" in enabled
    assert "start /b" in enabled
    assert "manage_process(action" not in disabled
    assert "start /b" not in disabled
