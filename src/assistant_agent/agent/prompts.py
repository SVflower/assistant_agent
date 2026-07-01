"""系统提示词。

对"笨模型"友好：明确说明工具使用方式、完成任务后如何收尾，
减少本地小模型跑飞的概率。
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime

SYSTEM_PROMPT = """你是一个跑在用户本地机器上的任务执行 Agent，擅长编码与开发类任务。

你可以调用工具来读写文件、列目录、执行 shell 命令，从而真正完成任务，而不只是给建议。

工作方式：
- 把用户的目标拆成具体步骤，一步步用工具完成。
- 每次需要信息或要改动时，调用合适的工具；拿到结果后再决定下一步。
- 不要假设文件内容或命令结果——先用工具确认。
- 危险操作（删除、覆盖、移动等）会请求用户确认，被拒绝时换一个安全的做法。

完成任务后：
- 不要再调用工具，直接用简洁的自然语言回复用户，说明你做了什么、结果如何。
- 如果任务失败或被阻塞，如实说明原因，不要假装成功。

保持简洁、直接。用用户使用的语言回复。"""


def _runtime_context() -> str:
    """构造运行环境说明，注入到系统提示词。

    告知模型当前操作系统与日期，避免它：
    - 用错命令语法（如在 Windows 上用 Unix 的 `date "+%A"`）
    - 因训练截止时间而答不出"今天几号/星期几"
    """
    system = platform.system()  # 'Windows' / 'Darwin' / 'Linux'
    if sys.platform == "win32":
        shell_hint = (
            "命令通过 cmd.exe 执行，请使用 Windows 命令语法（如 date /t、dir），"
            "不要用 Unix 专有命令。"
        )
    else:
        shell_hint = "命令通过 /bin/sh 执行，使用标准 Unix 命令语法。"
    now = datetime.now()
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    return (
        f"\n\n当前运行环境：\n"
        f"- 操作系统：{system}\n"
        f"- {shell_hint}\n"
        f"- 当前日期时间：{now:%Y-%m-%d %H:%M}（{weekday}）。"
        f"涉及\"今天/现在\"的问题直接用这个时间回答，不要说无法确定。\n"
        f"- 跨平台需求（如取日期时间）优先用 Python，而不是平台专有命令。"
    )


def build_system_prompt() -> str:
    """完整系统提示词 = 基础提示词 + 运行环境说明（运行时动态生成）。"""
    return SYSTEM_PROMPT + _runtime_context()
