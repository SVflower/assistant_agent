"""CLI Runtime 适配器；实际装配由公共 service.runtime 唯一拥有。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from assistant_agent.config.loader import find_config_file
from assistant_agent.contracts.errors import RuntimeConfigError, RuntimeInitializationError
from assistant_agent.execution import RunControl
from assistant_agent.service import AgentRuntime, RuntimeNotice, RuntimePolicy, create_runtime
from assistant_agent.ui.console import Console
from assistant_agent.ui.interaction import ConsoleInteractionAdapter

Runtime = AgentRuntime


def _show_notice(console: Console, notice: RuntimeNotice) -> None:
    details = ""
    if notice.code == "container_host_capabilities":
        raw_servers = notice.details.get("mcp_servers")
        servers = raw_servers if isinstance(raw_servers, list) else []
        suffix = f"（{', '.join(str(item) for item in servers)}）" if servers else ""
        details = f" 外部 MCP{suffix}"
    elif notice.code == "mcp_server_auto_approved":
        raw_servers = notice.details.get("servers")
        servers = raw_servers if isinstance(raw_servers, list) else []
        details = f"：{', '.join(str(item) for item in servers)}"
    message = f"（{notice.message}{details}）"
    if notice.level == "info":
        console.info(message)
    else:
        console.error(message)


def build_runtime(
    config_path: Path | None,
    console: Console,
    *,
    interactive: bool,
    interrupt_check: Callable[[], bool] | None = None,
    run_control: RunControl | None = None,
    provider: str | None = None,
    max_iterations: int | None = None,
) -> AgentRuntime:
    resolved = Path(config_path).expanduser().resolve() if config_path else find_config_file()
    if resolved is None:
        console.error("未找到 config.yaml。请复制 config.example.yaml 为 config.yaml 并填写。")
        raise typer.Exit(code=1)
    try:
        runtime = create_runtime(
            config_path=resolved,
            workspace_root=Path.cwd(),
            interaction=ConsoleInteractionAdapter(console),
            interactive=interactive,
            interrupt_check=interrupt_check,
            run_control=run_control,
            provider=provider,
            max_iterations=max_iterations,
            runtime_policy=RuntimePolicy.cli(),
        )
    except (RuntimeConfigError, RuntimeInitializationError) as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    for notice in runtime.notices:
        _show_notice(console, notice)
    console.set_show_reasoning(runtime.config.ui.show_reasoning)
    console.set_display_mode(runtime.config.ui.display_mode)
    console.set_context_limit(runtime.config.agent.max_context_tokens)
    backend = runtime.workspace.backend if runtime.workspace is not None else "host"
    console.banner(
        runtime.config.active,
        runtime.config.active_provider.model,
        runtime.config.permissions.mode,
        backend,
    )
    return runtime


__all__ = ["Runtime", "build_runtime"]
