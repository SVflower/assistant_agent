"""可恢复执行的 CLI 命令与 Session 补同步。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import typer

from assistant_agent.agent.recovery import RecoveryChoice, RunCoordinator
from assistant_agent.agent.run_state import ToolCallState
from assistant_agent.cli.setup import build_runtime
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.paths import resolve_run_dir
from assistant_agent.contracts.events import StepEvent
from assistant_agent.interaction import SafeDefaultInteractionPort
from assistant_agent.obs import sanitize_for_display
from assistant_agent.runtime import RunControl
from assistant_agent.service.sessions import SessionRuntime, sync_terminal_session
from assistant_agent.session.run_store import RunStore
from assistant_agent.session.store import Session
from assistant_agent.ui.console import Console

StreamRenderer = Callable[[Console, Iterator[StepEvent]], None]


def recovery_choice(console: Console, call: ToolCallState) -> RecoveryChoice:
    args = sanitize_for_display(call.arguments)
    answer = console.ask_question(
        f"工具 {call.name}（call_id={call.id}）执行结果未知。参数：{args}",
        ["retry（可能重复副作用）", "skip（注入跳过结果）", "abort（保持暂停）"],
    )
    if answer.startswith("retry"):
        return "retry"
    if answer.startswith("skip"):
        return "skip"
    return "abort"


def runs_command(config: Path | None, delete: str | None) -> None:
    console = Console()
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc
    store = RunStore(resolve_run_dir(cfg.agent.recovery.dir))
    metas = store.list()
    if delete:
        meta = next((item for item in metas if item.id == delete), None)
        if meta is None:
            console.error(f"Run 不存在：{delete}")
            raise typer.Exit(code=1)
        if meta.status in {"running", "paused"}:
            answer = (
                console.input(
                    f"[bold yellow]Run {delete} 尚未结束，输入 y 确认删除: [/bold yellow]"
                )
                .strip()
                .lower()
            )
            if answer not in {"y", "yes"}:
                console.info("已取消。")
                return
        store.delete(delete)
        console.info(f"已删除 Run {delete}。")
        return
    if not metas:
        console.info("暂无 Run checkpoint。")
        return
    lines = ["Run ID | 状态/阶段 | Session | 更新时间 | 任务"]
    lines.extend(
        f"{item.id} | {item.status}/{item.phase} | {item.session_id or '-'} | "
        f"{item.updated_at} | {item.preview}"
        for item in metas
    )
    console.info("\n".join(lines))


def resume_command(
    run_id: str,
    config: Path | None,
    provider: str | None,
    *,
    interrupt_check: Callable[[], bool] | None,
    run_control: RunControl | None = None,
    render_streamed: StreamRenderer,
) -> None:
    console = Console()
    try:
        cfg = load_config(config)
        preloaded = RunCoordinator.load(RunStore(resolve_run_dir(cfg.agent.recovery.dir)), run_id)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        console.error(f"无法恢复 Run：{exc}")
        raise typer.Exit(code=1) from exc

    selected = provider
    if selected is None and preloaded.state.provider in cfg.providers:
        selected = preloaded.state.provider
    tty = sys.stdin.isatty()
    with build_runtime(
        config,
        console,
        interactive=preloaded.state.interactive,
        interrupt_check=interrupt_check,
        run_control=run_control,
        provider=selected,
    ) as runtime:
        coordinator = RunCoordinator.load(runtime.run_store, run_id, logger=runtime.logger)
        runtime.logger.bind_session(coordinator.state.session_id)
        runtime.loop.set_interaction_available(tty)
        if not tty:
            safe_port = SafeDefaultInteractionPort()
            runtime.interaction = safe_port
            runtime.tool_context.interaction = safe_port
        if coordinator.state.session_id is not None:
            try:
                session = runtime.session_store.load(coordinator.state.session_id)
            except FileNotFoundError:
                session = Session(
                    id=coordinator.state.session_id,
                    created_at=coordinator.state.created_at,
                    updated_at=coordinator.state.updated_at,
                )
            session_runtime = SessionRuntime(runtime, session)
            execution = session_runtime.resume_run(run_id)
            if coordinator.load_info is not None and coordinator.load_info.warning:
                console.error(f"（恢复警告：{coordinator.load_info.warning}）")
            render_streamed(console, execution.events)
            return

        # 无 Session 的历史 run 命令保留兼容；API 正式入口始终使用 SessionRuntime。
        differences = coordinator.definition_differences(
            provider=runtime.config.active,
            model=runtime.config.active_provider.model,
            system_prompt=runtime.loop.system_prompt,
            tool_schemas=runtime.loop.tool_schemas,
        )
        if differences and coordinator.state.phase != "terminal":
            summary = ", ".join(item.field for item in differences)
            if not tty:
                console.error(f"Run 定义已变化（{summary}），非交互环境拒绝恢复。")
                raise typer.Exit(code=2)
            prompt = f"Run 定义已变化（{summary}），确认使用当前定义继续？"
            if console.confirm(prompt) == "deny":
                console.info("已取消。")
                return
            coordinator.accept_definitions(
                provider=runtime.config.active,
                model=runtime.config.active_provider.model,
                system_prompt=runtime.loop.system_prompt,
                tool_schemas=runtime.loop.tool_schemas,
            )
        coordinator.note_resume()
        if coordinator.load_info is not None and coordinator.load_info.warning:
            console.error(f"（恢复警告：{coordinator.load_info.warning}）")
        callback = (lambda call: recovery_choice(console, call)) if tty else None
        render_streamed(
            console,
            runtime.loop.resume(coordinator, recovery_check=callback),
        )
        try:
            sync_terminal_session(coordinator, runtime.session_store)
            runtime.run_store.prune(runtime.config.agent.recovery.max_completed_runs)
        except Exception as exc:  # noqa: BLE001 - Run 保持 unsynced，可再次 resume
            console.error(f"（Session 同步失败，可再次 resume 补做：{exc}）")
