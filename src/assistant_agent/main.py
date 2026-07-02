"""CLI 入口。

把配置、模型客户端、工具注册表、循环、UI 装配起来，提供：
- 单次任务：assistant-agent run "任务描述"
- 交互模式：assistant-agent chat
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from assistant_agent.agent.loop import AgentLoop, StepEvent
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.schema import AppConfig
from assistant_agent.llm.client import LLMClient
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import build_default_registry
from assistant_agent.ui.console import Console

app = typer.Typer(
    name="assistant-agent",
    help="模型后端可切换的通用任务 Agent。",
    add_completion=False,
)

# 任务执行期间的中断标志。Ctrl+C 时由信号处理器置起，AgentLoop 检查它以干净停止。
_interrupt = threading.Event()


@contextmanager
def _interruptible() -> Iterator[None]:
    """任务执行期间把 Ctrl+C(SIGINT) 转成"置中断标志"而非抛异常。

    退出时恢复默认处理器——这样在输入提示符处按 Ctrl+C 仍是正常的退出行为。
    signal.signal 只能在主线程调用（CLI 主流程满足）。
    """
    _interrupt.clear()
    previous = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, lambda *_: _interrupt.set())
    except ValueError:
        # 非主线程（如测试）无法设信号；此时不启用中断，直接放行。
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _run_streamed(console: Console, events: Iterator[StepEvent]) -> None:
    """在可中断上下文中渲染一次任务的流式事件。"""
    with _interruptible():
        console.render_stream(events)


def _setup(
    config_path: Path | None, console: Console, interactive: bool
) -> tuple[AppConfig, AgentLoop]:
    """加载配置并装配循环。失败时打印错误并退出。

    interactive：True 为 chat 多轮（允许澄清提问），False 为 run 单次（遇歧义自行假设）。
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    provider = config.active_provider
    client = LLMClient(provider)
    registry = build_default_registry()
    tool_ctx = ToolContext(
        confirm_dangerous_shell=config.tools.confirm_dangerous_shell,
        shell_timeout=config.tools.shell_timeout,
        confirm=console.confirm,
    )
    loop = AgentLoop(
        config,
        client,
        registry,
        tool_ctx,
        interactive=interactive,
        interrupt_check=_interrupt.is_set,
    )
    console.set_show_reasoning(config.ui.show_reasoning)
    console.banner(config.active, provider.model)
    return config, loop


@app.command()
def run(
    task: str = typer.Argument(..., help="要执行的任务描述"),
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    """执行单个任务后退出。"""
    console = Console()
    _, loop = _setup(config, console, interactive=False)
    console.user_echo(task)
    _run_streamed(console, loop.run(task))


@app.command()
def chat(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    """进入交互模式，连续对话（输入 exit/quit 退出）。"""
    console = Console()
    _, loop = _setup(config, console, interactive=True)
    console.info("进入交互模式，输入 exit 或 quit 退出。")
    while True:
        try:
            task = console._console.input("\n[bold green]你: [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.info("\n再见。")
            break
        if not task:
            continue
        if task.lower() in ("exit", "quit"):
            console.info("再见。")
            break
        _run_streamed(console, loop.run(task))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
