"""CLI 入口。

把配置、模型客户端、工具注册表、循环、UI 装配起来，提供：
- 单次任务：assistant-agent run "任务描述"
- 交互模式：assistant-agent chat
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from pathlib import Path

import typer

from assistant_agent.cli.commands import ChatContext, build_default_slash_registry
from assistant_agent.cli.init import run_init
from assistant_agent.cli.recovery import resume_command, runs_command, sessions_command
from assistant_agent.cli.reload import CLIRuntimeHolder
from assistant_agent.cli.setup import build_runtime
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.contracts.events import ItemEvent
from assistant_agent.execution import RunControl
from assistant_agent.service import SessionRuntime
from assistant_agent.ui.console import Console

app = typer.Typer(
    name="assistant-agent",
    help="模型后端可切换的通用任务 Agent。",
    add_completion=False,
)

# 第一次 Ctrl+C 请求可恢复暂停，第二次升级为强制取消。
_run_control = RunControl()


def _run_streamed(
    console: Console, events: Iterator[ItemEvent], run_control: RunControl | None = None
) -> None:
    """在可中断上下文中渲染一次任务的流式事件。"""
    control = run_control or _run_control
    control.reset()
    previous = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, lambda *_: control.request_interrupt())
    except ValueError:
        console.render_stream(events)
        return
    try:
        console.render_stream(events)
    finally:
        signal.signal(signal.SIGINT, previous)


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
        run_control=_run_control,
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
        run_control=_run_control,
        provider=provider,
        max_iterations=max_iterations,
    ) as rt:
        store = rt.session_store

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
            store.save(session, [], must_exist=False)
            console.info(f"新会话 {session.id}。输入 / 查看命令，exit/quit 退出。")

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
        session_runtime = SessionRuntime(rt, session)
        holder = CLIRuntimeHolder(rt, session_runtime)

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
            mcp_runtime=rt.mcp,
            skill_manager=rt.skill_manager,
            skills_config_store=rt.skills_config_store,
            mcp_service=rt.mcp_service,
            tool_context=rt.tool_context,
        )

        def reload_runtime(target: str) -> str:
            def factory(control: RunControl):
                return build_runtime(
                    config,
                    console,
                    interactive=True,
                    run_control=control,
                    provider=holder.runtime.config.active,
                    max_iterations=max_iterations,
                    show_banner=False,
                )

            try:
                return holder.reload(target, ctx, factory)  # type: ignore[arg-type]
            except typer.Exit as exc:
                raise RuntimeError("候选 Runtime 初始化失败，当前 generation 保持可用。") from exc

        def refresh_changed_skills() -> None:
            try:
                message = holder.reload_if_skills_changed(
                    ctx,
                    lambda control: build_runtime(
                        config,
                        console,
                        interactive=True,
                        run_control=control,
                        provider=holder.runtime.config.active,
                        max_iterations=max_iterations,
                        show_banner=False,
                    ),
                )
            except (RuntimeError, typer.Exit) as exc:
                console.error(f"Skill 自动刷新失败，继续使用当前 Runtime：{exc}")
                return
            if message is not None:
                console.command_info(message)

        ctx.reload_runtime = reload_runtime
        registry = build_default_slash_registry()
        console.set_slash_commands(registry.descriptions())

        while True:
            try:
                task = console.chat_input().strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not task:
                continue
            refresh_changed_skills()
            if task.lower() in ("exit", "quit"):
                break
            if task.startswith("/"):
                previous_session_id = ctx.session.id
                registry.dispatch(task, ctx)
                if ctx.should_exit:
                    break
                if ctx.session.id != previous_session_id:
                    holder.session_runtime = SessionRuntime(holder.runtime, ctx.session)
                continue
            try:
                execution = holder.session_runtime.start_run(task)
                console.show_run_id(execution.run_id)
                _run_streamed(console, execution.events, holder.runtime.run_control)  # type: ignore[arg-type]
                state = holder.runtime.run_store.load(execution.run_id).document
                ctx.session = holder.session_runtime.session
                if state["status"] != "completed" and console.display_mode != "verbose":
                    console.show_run_id(execution.run_id, force=True)
                if state["status"] == "paused":
                    console.error(
                        "当前 Run 已暂停。为避免会话分叉，chat 已停止；请按 Run ID 恢复。"
                    )
                    break
            except Exception as exc:  # noqa: BLE001 - 保留 checkpoint，后续可 resume 补同步
                console.error(f"（自动保存失败，已跳过：{exc}）")
                console.error("为避免 Session/Run 分叉，已停止当前 chat；请按 Run ID 恢复。")
                break
        if holder.runtime is not rt:
            holder.runtime.close("cli_chat_exit")
    # 单一出口打印一次；ctx.session 会随 /clear 更新，显示实际结束的会话。
    console.info(f"\n已结束会话 {ctx.session.id}。")
    console.info("恢复此会话：")
    console.info(f"assistant-agent chat --resume {ctx.session.id}")


@app.command()
def sessions(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    delete: str | None = typer.Option(None, "--delete", "-d", help="删除指定会话 id"),
    force: bool = typer.Option(False, "--force", help="强制删除含活动 Run 的会话"),
) -> None:
    """列出历史会话；--delete <id> 删除指定会话。"""
    sessions_command(config, delete, force=force)


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
        interrupt_check=None,
        run_control=_run_control,
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
