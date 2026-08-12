"""终端展示的纯格式化辅助函数（无状态，从 console.py 抽出以保持模块聚焦）。"""

from __future__ import annotations

import json
from typing import Any

from rich import box
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from assistant_agent.tools.permissions import permission_mode_label


def read_input(prompt: str = "") -> str:
    """用纯文本提示符读取一行输入。

    Linux/readline 下，Rich 的"打印带色提示符 + 空 input()"会让退格越过输入起点、
    删掉提示符本身。改用纯文本 prompt 传给内置 input()，让 readline 正确保护提示符
    （代价：提示符无颜色，换取正确行编辑）。
    """
    plain = Text.from_markup(prompt).plain if prompt else ""
    return input(plain)


def build_banner(
    provider_name: str,
    model: str,
    cwd: str,
    permission_mode: str,
    execution_backend: str,
    *,
    verbose: bool = False,
) -> Panel:
    """构建有明确层级的紧凑启动面板。"""
    style = "red" if permission_mode == "unrestricted" else "yellow"
    info = Text()
    info.append("模型  ", style="dim")
    info.append(model, style="cyan")
    if verbose:
        info.append("\n后端  ", style="dim")
        info.append(provider_name, style="cyan")
    else:
        info.append(f"  · {provider_name}", style="dim")
    info.append("\n位置  ", style="dim")
    info.append(cwd, style="green")
    info.append("\n权限  ", style="dim")
    known_modes = {"readonly", "workspace", "strict", "unrestricted"}
    label = (
        permission_mode_label(permission_mode)
        if permission_mode in known_modes
        else permission_mode
    )
    info.append(f"{label}（{permission_mode}）", style=style)
    info.append("  · 应用策略", style=style)
    execution_label, execution_style = _execution_label(execution_backend)
    info.append("\n执行  ", style="dim")
    info.append(execution_label, style=execution_style)
    return Panel(
        info,
        title=Text("Assistant Agent", style="bold"),
        title_align="left",
        border_style="blue",
        expand=False,
        padding=(0, 1),
    )


def _execution_label(backend: str) -> tuple[str, str]:
    if backend == "container":
        return "container  · Shell/Git 容器隔离", "green"
    if backend == "confined":
        return "workspace  · 路径受限，非 OS 沙箱", "yellow"
    return "host  · 宿主执行，无 OS 沙箱", "red"


def build_response_panel(text: str) -> Panel:
    """构建带稳定 Agent 标识的最终回答区域。"""
    return Panel(
        Markdown(text),
        title=Text("$ Assistant", style="bold cyan"),
        title_align="left",
        border_style="cyan",
        box=box.HORIZONTALS,
        padding=(0, 2),
        expand=True,
    )


def build_turn_status(
    model: str,
    elapsed: str,
    total_in: int,
    total_out: int,
    prompt_tokens: int,
    context_limit: int,
    width: int,
) -> Text:
    """构建单行任务状态带；宽度不足时在尾部省略。"""
    bg = "on #1a1a2e"
    status = Text(style=bg)
    status.append(f" {model.rsplit('/', 1)[-1] or model} ", style=f"bold cyan {bg}")
    status.append("│ ", style=f"dim {bg}")
    status.append(f"token {format_usage(total_in, total_out)} ", style=f"white {bg}")
    status.append("│ ", style=f"dim {bg}")
    context = Text.from_markup(format_context(prompt_tokens, context_limit)).plain
    ratio = _context_ratio(prompt_tokens, context_limit)
    context_style = "red" if ratio >= 0.9 else "yellow" if ratio >= 0.8 else "green"
    status.append(f"{context} ", style=f"{context_style} {bg}")
    status.append("│ ", style=f"dim {bg}")
    status.append(f"{elapsed} ", style=f"white {bg}")
    max_width = max(int(width), 1)
    status.truncate(max_width, overflow="ellipsis", pad=False)
    return status


def _context_ratio(prompt_tokens: int, context_limit: int) -> float:
    return prompt_tokens / context_limit if context_limit > 0 else 0.0


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


def format_usage(total_in: int, total_out: int) -> str:
    """累计 token（成本视角）：跨轮求和的输入/输出/合计。"""
    return f"↑{total_in} ↓{total_out} 共 {total_in + total_out}"


def format_context(prompt_tokens: int, limit: int) -> str:
    """上下文占用（容量视角）：最后一轮 prompt / 窗口预算，近满变色。"""
    if limit <= 0:
        return f"上下文 {prompt_tokens}"
    pct = round(prompt_tokens / limit * 100)
    color = "red" if pct >= 90 else "yellow" if pct >= 80 else "dim"
    return f"[{color}]上下文 {prompt_tokens}/{limit}（{pct}%）[/{color}]"


def format_elapsed(seconds: float) -> str:
    """人类可读的耗时：<60s 显示秒，否则分秒。"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"


def build_providers_table(rows: list[tuple[str, str, str]]) -> Table:
    """构建 provider 列表表格。rows 为 (名字, 模型, 云端/本地)。"""
    table = Table(title="可用 provider", border_style="blue")
    table.add_column("provider", style="cyan", no_wrap=True)
    table.add_column("模型")
    table.add_column("类型", style="dim")
    for name, model, kind in rows:
        table.add_row(name, model, kind)
    return table


def build_sessions_table(metas: list[Any]) -> Table:
    """构建历史会话表格。metas 为 SessionMeta 序列。"""
    table = Table(title="历史会话", show_lines=False, border_style="blue")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("更新时间", style="dim")
    table.add_column("消息数", justify="right")
    table.add_column("首条内容")
    for m in metas:
        table.add_row(m.id, m.updated_at, str(m.message_count), m.preview)
    return table
