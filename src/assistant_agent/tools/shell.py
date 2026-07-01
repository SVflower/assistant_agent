"""shell 命令工具。

安全策略（见 DESIGN.md）：删除/覆盖/移动等危险操作执行前需用户确认，
普通命令直接执行。确认通过注入的回调完成，工具本身不依赖 UI。
"""

from __future__ import annotations

import locale
import re
import subprocess
import sys
from typing import Any

from assistant_agent.tools.base import Tool, ToolContext, ToolResult

# 危险操作的启发式匹配。命中则在执行前要求确认。
# 宁可多问一次，也不要漏过破坏性命令。
_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\b"),
    re.compile(r"\brmdir\b"),
    re.compile(r"\bmv\b"),
    re.compile(r"\bdd\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\btruncate\b"),
    re.compile(r"(?<!>)>\s*[^>\s|]"),  # 重定向覆盖（> file，但排除 >> 追加）
    re.compile(r"\bgit\s+(reset|clean|checkout\s+--|push\s+--force|push\s+-f)"),
    re.compile(r"\b(shutdown|reboot|kill|killall|pkill)\b"),
    re.compile(r"\b(chmod|chown)\b.*-R"),
    re.compile(r"\bdel\b", re.IGNORECASE),  # Windows
    re.compile(r"\bformat\b", re.IGNORECASE),  # Windows
    re.compile(r"Remove-Item", re.IGNORECASE),  # PowerShell
]


def is_dangerous(command: str) -> bool:
    """判断命令是否命中危险模式。"""
    return any(p.search(command) for p in _DANGEROUS_PATTERNS)


def _decode(raw: bytes | None) -> str:
    """把命令输出的 bytes 按平台编码解码，容错不崩。

    Windows 的 cmd 输出通常是 GBK/本地代码页，用 UTF-8 直接解会乱码。
    这里先试 UTF-8，失败再回退到平台默认编码，最后兜底 replace。
    """
    if not raw:
        return ""
    encodings = ["utf-8"]
    if sys.platform == "win32":
        # 本地代码页（简中一般是 gbk/cp936）
        encodings.append(locale.getpreferredencoding(False))
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(encodings[-1], errors="replace")


class ShellTool(Tool):
    name = "run_shell"
    description = (
        "执行一条 shell 命令并返回 stdout/stderr。"
        "用于运行测试、查看文件、git 等操作。危险命令会先请求用户确认。"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的完整 shell 命令"}},
            "required": ["command"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if not command or not command.strip():
            return ToolResult.error("缺少参数 command")

        if ctx.confirm_dangerous_shell and is_dangerous(command):
            allowed = ctx.request_confirm(
                "run_shell", f"即将执行可能有风险的命令：\n  {command}"
            )
            if not allowed:
                return ToolResult.error(f"用户拒绝执行命令：{command}")

        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                # 切断 stdin：交互式命令（date、more、pause 等）读到 EOF 会立即
                # 失败退出，而不是停下等输入、阻塞到超时。
                stdin=subprocess.DEVNULL,
                timeout=ctx.shell_timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.error(
                f"命令超时（>{ctx.shell_timeout}s）：{command}。"
                "若是交互式命令（需要键盘输入），请改用非交互写法。"
            )
        except OSError as exc:
            return ToolResult.error(f"无法执行命令：{exc}")

        stdout = _decode(completed.stdout)
        stderr = _decode(completed.stderr)
        parts: list[str] = [f"退出码：{completed.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"stderr:\n{stderr.rstrip()}")
        output = "\n".join(parts)
        # 非零退出码不当作工具错误：把结果交给模型判断如何应对。
        return ToolResult.ok(output)
