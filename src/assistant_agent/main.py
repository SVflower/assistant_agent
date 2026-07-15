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

from assistant_agent.agent.events import StepEvent
from assistant_agent.cli.commands import ChatContext, build_default_slash_registry
from assistant_agent.cli.init import run_init
from assistant_agent.cli.recovery import (
    resume_command,
    runs_command,
    sync_terminal_session,
)
from assistant_agent.cli.setup import build_runtime
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.session.store import SessionStore
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
    quiet: bool = typer.Option(False, "--quiet", "-q", help="仅输出最终结果或错误"),
) -> None:
    """执行单个任务后退出。"""
    console = Console(display_mode="quiet" if quiet else None)
    with build_runtime(
        config,
        console,
        interactive=False,
        interrupt_check=_interrupt.is_set,
        provider=provider,
        max_iterations=max_iterations,
    ) as rt:
        console.user_echo(task)
        rt.logger.task(task)
        coordinator = rt.new_run(task)
        if coordinator is not None:
            console.show_run_id(coordinator.run_id)
        _run_streamed(console, rt.loop.run(task, coordinator=coordinator))
        if coordinator is not None:
            if coordinator.state.status != "completed" and console.display_mode != "verbose":
                console.show_run_id(coordinator.run_id, force=True)
            rt.run_store.prune(rt.config.agent.recovery.max_completed_runs)


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
    对话中输入 / 或 /help 查看所有命令（/model 切模型、/clear 新会话等）。
    """
    console = Console()
    with build_runtime(
        config,
        console,
        interactive=True,
        interrupt_check=_interrupt.is_set,
        provider=provider,
        max_iterations=max_iterations,
    ) as rt:
        store = SessionStore()

        if resume:
            try:
                session = store.load(resume)
            except (FileNotFoundError, ValueError) as exc:
                console.error(f"无法恢复会话：{exc}")
                raise typer.Exit(code=1) from exc
            rt.loop.load_history(session.messages)
            rt.loop.load_checkpoint(session.compaction_checkpoint)  # M8b：resume 不重复摘要
            session.provider = rt.config.active
            session.model = rt.config.active_provider.model
            console.info(
                f"已恢复会话 {resume}（{len(session.messages)} 条消息），"
                f"沿用当前配置模型 {session.model}。"
            )
        else:
            session = store.new_session(
                provider=rt.config.active, model=rt.config.active_provider.model
            )
            store.save(session, [])
            if console.display_mode == "verbose":
                console.info(f"新会话 {session.id}。输入 /help 查看命令，exit/quit 退出。")

        unfinished = [
            item
            for item in rt.run_store.list()
            if item.session_id == session.id and item.status in {"running", "paused"}
        ]
        if unfinished:
            run_id = unfinished[0].id
            console.error(
                f"会话 {session.id} 存在未完成 Run {run_id}；请先执行 "
                f"assistant-agent resume {run_id}。"
            )
            raise typer.Exit(code=1)
        rt.logger.bind_session(session.id)

        mcp_servers = rt.mcp.server_summary() if rt.mcp else []
        ctx = ChatContext(
            rt.config,
            rt.loop,
            console,
            store,
            session,
            rt.logger,
            skills=rt.skills_meta(),
            mcp_servers=mcp_servers,
        )
        registry = build_default_slash_registry()

        while True:
            try:
                task = console.input("\n[bold green]你: [/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not task:
                continue
            if task.lower() in ("exit", "quit"):
                break
            if task.startswith("/"):
                registry.dispatch(task, ctx)
                if ctx.should_exit:
                    break
                continue
            rt.logger.task(task)
            coordinator = rt.new_run(task, ctx.session.id)
            if coordinator is not None:
                console.show_run_id(coordinator.run_id)
            _run_streamed(console, rt.loop.run(task, coordinator=coordinator))
            try:
                if coordinator is not None:
                    if (
                        coordinator.state.status != "completed"
                        and console.display_mode != "verbose"
                    ):
                        console.show_run_id(coordinator.run_id, force=True)
                    synced = sync_terminal_session(coordinator, store, ctx.session)
                    if synced is not None:
                        ctx.session = synced
                    rt.run_store.prune(rt.config.agent.recovery.max_completed_runs)
                else:
                    ctx.session.compaction_checkpoint = rt.loop.export_checkpoint()
                    store.save(ctx.session, rt.loop.export_history())
            except Exception as exc:  # noqa: BLE001 - 保留 checkpoint，后续可 resume 补同步
                console.error(f"（自动保存失败，已跳过：{exc}）")
                if coordinator is not None:
                    console.error("为避免 Session/Run 分叉，已停止当前 chat；请按 Run ID 恢复。")
                    break
    # 单一出口打印一次；带前导换行，Ctrl+C/D 中断后也能干净换行
    console.info("\n再见。")


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
def runs(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    delete: str | None = typer.Option(None, "--delete", "-d", help="删除指定 Run ID"),
) -> None:
    """列出可恢复 Run；--delete 删除指定记录。"""
    runs_command(config, delete)


@app.command("resume")
def resume_run(
    run_id: str = typer.Argument(..., help="要恢复的 Run ID"),
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="覆盖保存的 provider（会触发兼容确认）"
    ),
) -> None:
    """从最近有效 checkpoint 恢复一次 Run。"""
    resume_command(
        run_id,
        config,
        provider,
        interrupt_check=_interrupt.is_set,
        render_streamed=_run_streamed,
    )


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


@app.command()
def init(
    config: Path | None = typer.Option(None, "--config", "-c", help="生成的配置文件路径"),
) -> None:
    """交互式配置向导：选后端、配 key/端点、生成 config.yaml。"""
    console = Console()
    code = run_init(console, config)
    if code != 0:
        raise typer.Exit(code=code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
