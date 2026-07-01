"""CLI 入口。

把配置、模型客户端、工具注册表、循环、UI 装配起来，提供：
- 单次任务：assistant-agent run "任务描述"
- 交互模式：assistant-agent chat
"""

from __future__ import annotations

from pathlib import Path

import typer

from assistant_agent.agent.loop import AgentLoop
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
    loop = AgentLoop(config, client, registry, tool_ctx, interactive=interactive)
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
    console.render_stream(loop.run(task))


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
        console.render_stream(loop.run(task))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
