"""可恢复执行的 CLI 命令与 Session 补同步。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import typer

from assistant_agent.application.models import Session
from assistant_agent.application.runs import resume_standalone_run
from assistant_agent.cli.setup import build_runtime
from assistant_agent.config.loader import find_config_file
from assistant_agent.contracts.errors import RuntimeConfigError, SessionRunConflictError
from assistant_agent.contracts.events import StepEvent
from assistant_agent.interaction import SafeDefaultInteractionPort
from assistant_agent.runtime import RunControl
from assistant_agent.service import AgentService, SessionRuntime
from assistant_agent.ui.console import Console

StreamRenderer = Callable[[Console, Iterator[StepEvent]], None]


def _build_service(config: Path | None, console: Console) -> AgentService:
    resolved = Path(config).expanduser().resolve() if config else find_config_file()
    if resolved is None:
        console.error("未找到 config.yaml。请复制 config.example.yaml 为 config.yaml 并填写。")
        raise typer.Exit(code=1)
    try:
        return AgentService(config_path=resolved, workspace_root=Path.cwd())
    except RuntimeConfigError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc


def runs_command(config: Path | None, delete: str | None) -> None:
    console = Console()
    service = _build_service(config, console)
    metas = service.list_runs()
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
        service.delete_run(delete, force=True)
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
    service = _build_service(config, console)
    try:
        resume_info = service.inspect_run(run_id)
    except (FileNotFoundError, ValueError) as exc:
        console.error(f"无法恢复 Run：{exc}")
        raise typer.Exit(code=1) from exc

    selected = provider
    if selected is None and resume_info.provider:
        selected = resume_info.provider
    with build_runtime(
        config,
        console,
        interactive=resume_info.interactive,
        interrupt_check=interrupt_check,
        run_control=run_control,
        provider=selected,
    ) as runtime:
        tty = sys.stdin.isatty()
        runtime.loop.set_interaction_available(tty)
        if not tty:
            safe_port = SafeDefaultInteractionPort()
            runtime.interaction = safe_port
            runtime.tool_context.interaction = safe_port
        if resume_info.session_id is not None:
            try:
                session = runtime.session_store.load(resume_info.session_id)
            except FileNotFoundError:
                session = Session(
                    id=resume_info.session_id,
                    created_at=resume_info.created_at,
                    updated_at=resume_info.updated_at,
                )
            session_runtime = SessionRuntime(runtime, session)
            execution = session_runtime.resume_run(run_id)
            if execution.warning:
                console.error(f"（恢复警告：{execution.warning}）")
            render_streamed(console, execution.events)
            return

        try:
            execution = resume_standalone_run(runtime, run_id)
        except SessionRunConflictError as exc:
            console.error(str(exc))
            raise typer.Exit(code=2) from exc
        if execution.warning:
            console.error(f"（恢复警告：{execution.warning}）")
        render_streamed(console, execution.events)
