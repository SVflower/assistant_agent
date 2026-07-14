"""运行时装配（Runtime）。

把配置加载、客户端、工具注册表、技能、MCP、日志、循环的组装从 main 抽出来（还 D7）。
Runtime 是上下文管理器，统管 logger 会话与 MCP 线程/子进程的生命周期——退出时干净关闭。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import typer

from assistant_agent.agent.loop import AgentLoop
from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.config.schema import AppConfig, MCPConfig, SkillsConfig
from assistant_agent.llm.client import LLMClient
from assistant_agent.mcp import MCPManager
from assistant_agent.obs import NullLogger, create_logger
from assistant_agent.session.store import new_session_id
from assistant_agent.skills import LoadSkillTool, SkillStore
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry, build_default_registry
from assistant_agent.ui.console import Console


def _discover_skills(cfg: SkillsConfig) -> SkillStore:
    """按配置发现技能。禁用时返回空 store（不扫描、不注入）。

    默认目录：./.assistant_agent/skills（项目级，优先）与 ~/.assistant_agent/skills（个人级）。
    """
    if not cfg.enabled:
        return SkillStore({})
    if cfg.dirs:
        dirs = [Path(d).expanduser() for d in cfg.dirs]
    else:
        dirs = [
            Path.cwd() / ".assistant_agent" / "skills",
            Path.home() / ".assistant_agent" / "skills",
        ]
    return SkillStore.discover(dirs)


def _start_mcp(cfg: MCPConfig, console: Console, registry: ToolRegistry) -> MCPManager | None:
    """连接 MCP server、注册其工具、打印警告。禁用或无 server 时返回 None。"""
    if not cfg.enabled or not cfg.servers:
        return None
    manager = MCPManager(cfg, NullLogger())
    tools = manager.start()
    for tool in tools:
        registry.register(tool)
    for warning in manager.warnings:
        console.error(f"（MCP）{warning}")
    if tools:
        console.info(f"（MCP）已接入 {len(tools)} 个外部工具。")
    return manager


@dataclass
class Runtime:
    """一次运行的装配结果 + 生命周期管理。用作上下文管理器。"""

    config: AppConfig
    loop: AgentLoop
    logger: NullLogger
    skill_store: SkillStore
    mcp: MCPManager | None = None
    _closed: bool = field(default=False, init=False)

    def skills_meta(self) -> list[tuple[str, str]]:
        return [(m.name, m.description) for m in self.skill_store.list()]

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self, reason: str = "") -> None:
        """关闭 MCP（线程/子进程）并记 session_end。幂等。"""
        if self._closed:
            return
        self._closed = True
        if self.mcp is not None:
            self.mcp.close()
        self.logger.session_end(reason=reason)


def build_runtime(
    config_path: Path | None,
    console: Console,
    *,
    interactive: bool,
    interrupt_check: Callable[[], bool],
    provider: str | None = None,
    max_iterations: int | None = None,
) -> Runtime:
    """加载配置并装配整个运行时。失败时打印错误并退出（typer.Exit）。

    interactive：True 为 chat 多轮（允许澄清），False 为 run 单次（遇歧义自行假设）。
    provider：非空则覆盖 active（临时后端，不改文件）。max_iterations：非空覆盖最大轮数。
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

    client = LLMClient(config.active_provider)
    registry = build_default_registry()

    # 技能发现（L1）：有技能才注册 load_skill 并注入系统提示词。
    skill_store = _discover_skills(config.skills)
    skills = skill_store.list()
    if skills:
        registry.register(LoadSkillTool(skill_store))
    skill_meta = [(m.name, m.description) for m in skills]
    system_prompt = build_system_prompt(interactive, skills=skill_meta or None)

    logger = create_logger(config.logging, new_session_id())
    logger.session_start(
        provider=config.active,
        model=config.active_provider.model,
        mode="chat" if interactive else "run",
        cwd=str(Path.cwd()),
    )

    # MCP（M7b）：连接 server、注册外部工具。禁用/无 server 时 None。
    mcp = _start_mcp(config.mcp, console, registry)

    tool_ctx = ToolContext(
        confirm_dangerous_shell=config.tools.confirm_dangerous_shell,
        shell_timeout=config.tools.shell_timeout,
        confirm=console.confirm,
        ask=console.ask_question,
        logger=logger,
        max_output_chars=config.tools.max_output_chars,
    )
    continue_check = console.confirm_continue if interactive else None
    loop = AgentLoop(
        config, client, registry, tool_ctx,
        interactive=interactive,
        interrupt_check=interrupt_check,
        continue_check=continue_check,
        system_prompt=system_prompt,
    )
    console.set_show_reasoning(config.ui.show_reasoning)
    console.set_context_limit(config.agent.max_context_tokens)
    console.banner(config.active, config.active_provider.model)
    return Runtime(config=config, loop=loop, logger=logger, skill_store=skill_store, mcp=mcp)
