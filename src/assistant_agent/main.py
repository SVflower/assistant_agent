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
from assistant_agent.session.store import SessionStore
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


def _build_client(config: AppConfig) -> LLMClient:
    """按当前 active provider 构建 LLMClient。"""
    return LLMClient(config.active_provider)


def _setup(
    config_path: Path | None,
    console: Console,
    interactive: bool,
    provider: str | None = None,
    max_iterations: int | None = None,
) -> tuple[AppConfig, AgentLoop]:
    """加载配置并装配循环。失败时打印错误并退出。

    interactive：True 为 chat 多轮（允许澄清提问），False 为 run 单次（遇歧义自行假设）。
    provider：非空则覆盖 config 的 active（临时指定后端，不改文件）；非法名报错列出可选。
    max_iterations：非空则覆盖 config 的最大轮数。
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    if provider is not None:
        if provider not in config.providers:
            available = ", ".join(sorted(config.providers))
            console.error(f"未知 provider：{provider}。可选：{available}")
            raise typer.Exit(code=1)
        config.active = provider

    if max_iterations is not None:
        config.agent.max_iterations = max_iterations

    client = _build_client(config)
    registry = build_default_registry()
    tool_ctx = ToolContext(
        confirm_dangerous_shell=config.tools.confirm_dangerous_shell,
        shell_timeout=config.tools.shell_timeout,
        confirm=console.confirm,
        ask=console.ask_question,
    )
    # 交互模式(chat)：用尽轮数时问用户是否继续；单次(run)：不问，优雅终止。
    continue_check = console.confirm_continue if interactive else None
    loop = AgentLoop(
        config,
        client,
        registry,
        tool_ctx,
        interactive=interactive,
        interrupt_check=_interrupt.is_set,
        continue_check=continue_check,
    )
    console.set_show_reasoning(config.ui.show_reasoning)
    console.banner(config.active, config.active_provider.model)
    return config, loop


@app.command()
def run(
    task: str = typer.Argument(..., help="要执行的任务描述"),
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="临时指定 provider（覆盖 config 的 active）"
    ),
    max_iterations: int | None = typer.Option(
        None, "--max-iterations", help="最大工具调用轮数（覆盖 config）"
    ),
) -> None:
    """执行单个任务后退出。"""
    console = Console()
    _, loop = _setup(
        config, console, interactive=False, provider=provider, max_iterations=max_iterations
    )
    console.user_echo(task)
    _run_streamed(console, loop.run(task))


@app.command()
def chat(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    resume: str | None = typer.Option(None, "--resume", "-r", help="恢复指定会话 id 并续接"),
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="临时指定 provider（覆盖 config 的 active）"
    ),
    max_iterations: int | None = typer.Option(
        None, "--max-iterations", help="最大工具调用轮数（覆盖 config）"
    ),
) -> None:
    """进入交互模式，连续对话（输入 exit/quit 退出）。

    默认新建会话并自动保存；--resume <id> 恢复历史会话续接。
    对话中输入 /model 可切换模型（保留上下文）。
    """
    console = Console()
    config_obj, loop = _setup(
        config, console, interactive=True, provider=provider, max_iterations=max_iterations
    )
    store = SessionStore()

    if resume:
        try:
            session = store.load(resume)
        except (FileNotFoundError, ValueError) as exc:
            console.error(f"无法恢复会话：{exc}")
            raise typer.Exit(code=1) from exc
        loop.load_history(session.messages)
        console.info(f"已恢复会话 {resume}（{len(session.messages)} 条消息），继续对话。")
    else:
        session = store.new_session(
            provider=config_obj.active, model=config_obj.active_provider.model
        )
        console.info(f"新会话 {session.id}。输入 exit 或 quit 退出；/model 切换模型。")

    while True:
        try:
            task = console.input("\n[bold green]你: [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.info("\n再见。")
            break
        if not task:
            continue
        if task.lower() in ("exit", "quit"):
            console.info("再见。")
            break
        if task.split()[0] == "/model":
            _handle_model_command(task, config_obj, loop, console)
            continue
        _run_streamed(console, loop.run(task))
        # 每轮结束自动保存（覆盖写整个会话文件）
        store.save(session, loop.export_history())


def _handle_model_command(
    task: str, config: AppConfig, loop: AgentLoop, console: Console
) -> None:
    """处理 chat 内的 /model：切换 provider，保留当前对话历史。"""
    parts = task.split(maxsplit=1)
    names = sorted(config.providers)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if arg:
        target = arg
    else:
        # 无参：弹菜单选择（复用 ask_question）；非交互下提示用带名形式
        import sys

        if not sys.stdin.isatty():
            console.info(f"非交互环境，请用 /model <名>。可选：{', '.join(names)}")
            return
        choices = [f"{n}（当前）" if n == config.active else n for n in names]
        picked = console.ask_question("切换到哪个 provider？", choices)
        target = picked.replace("（当前）", "").strip()

    if target not in config.providers:
        console.error(f"未知 provider：{target}。可选：{', '.join(names)}")
        return
    if target == config.active:
        console.info(f"已在使用 {target}，无需切换。")
        return

    config.active = target
    loop.set_client(_build_client(config))
    console.info(f"已切换到 {target}（{config.active_provider.model}），对话上下文保留。")


@app.command()
def sessions(
    delete: str | None = typer.Option(None, "--delete", "-d", help="删除指定会话 id"),
) -> None:
    """列出历史会话；--delete <id> 删除指定会话。"""
    console = Console()
    store = SessionStore()

    if delete:
        meta = next((m for m in store.list() if m.id == delete), None)
        if meta is None:
            console.error(f"会话不存在：{delete}")
            raise typer.Exit(code=1)
        # 删除不可逆，先确认
        answer = (
            console.input(
                f"[bold yellow]确认删除会话 {delete}（{meta.preview}）？输入 y 确认: [/bold yellow]"
            )
            .strip()
            .lower()
        )
        if answer in ("y", "yes"):
            store.delete(delete)
            console.info(f"已删除会话 {delete}。")
        else:
            console.info("已取消。")
        return

    metas = store.list()
    if not metas:
        console.info("暂无历史会话。")
        return
    console.print_sessions(metas)


@app.command()
def providers(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    """列出配置里所有可用的 provider（名字/模型/云端或本地）。"""
    console = Console()
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc
    rows = []
    for name in sorted(cfg.providers):
        p = cfg.providers[name]
        kind = "本地" if p.api_base else "云端"
        active = " (当前)" if name == cfg.active else ""
        rows.append((name + active, p.model, kind))
    console.print_providers(rows)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
