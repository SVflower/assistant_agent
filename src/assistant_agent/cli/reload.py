"""CLI 专用的完整 Runtime 代际切换。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assistant_agent.application.runtime import AgentRuntime
from assistant_agent.cli.commands import ChatContext
from assistant_agent.config.paths import legacy_project_skills_dirs
from assistant_agent.contracts.capabilities import RuntimeCapabilities
from assistant_agent.execution import RunControl
from assistant_agent.service import SessionRuntime

ReloadTarget = Literal["skills", "mcp", "all"]


@dataclass
class CLIRuntimeHolder:
    runtime: AgentRuntime
    session_runtime: SessionRuntime
    generation: int = 1
    skill_snapshot: tuple[tuple[str, int, int], ...] = ()

    def __post_init__(self) -> None:
        self.skill_snapshot = _skill_snapshot(self.runtime.skill_manager)

    def reload_if_skills_changed(
        self,
        ctx: ChatContext,
        factory: Callable[[RunControl], AgentRuntime],
    ) -> str | None:
        current = _skill_snapshot(self.runtime.skill_manager)
        if current == self.skill_snapshot:
            return None
        return self.reload("skills", ctx, factory)

    def reload(
        self,
        target: ReloadTarget,
        ctx: ChatContext,
        factory: Callable[[RunControl], AgentRuntime],
    ) -> str:
        """候选完整可用后才交换；构建失败时旧代不发生任何变化。"""
        if self.session_runtime.active_run_id is not None:
            raise RuntimeError("当前 Run 正在执行，拒绝刷新 Runtime。请等待任务结束或取消后重试。")
        pending = getattr(self.runtime.interaction, "pending_requests", None)
        if callable(pending) and pending():
            raise RuntimeError("当前 Runtime 正在等待交互，拒绝刷新。请先完成或取消交互。")

        old_runtime = self.runtime
        old_session_runtime = self.session_runtime
        old_capabilities = old_runtime.capabilities_snapshot()
        candidate = factory(RunControl())
        try:
            candidate_session_runtime = SessionRuntime(candidate, ctx.session)
            new_capabilities = candidate.capabilities_snapshot()
            if new_capabilities is None:
                raise RuntimeError("候选 Runtime 未提供能力快照")
            binding = _context_binding(candidate)
        except BaseException:
            candidate.close("cli_reload_rollback")
            raise

        # 上面已完成所有可能失败的候选读取；以下交换只做内存赋值。
        self.runtime = candidate
        self.session_runtime = candidate_session_runtime
        self.generation += 1
        self.skill_snapshot = _skill_snapshot(candidate.skill_manager)
        _bind_context(ctx, self.generation, binding)
        old_session_runtime.close()
        return _reload_summary(
            target,
            self.generation,
            old_capabilities,
            new_capabilities,
            getattr(candidate.skill_store, "report", None),
        )


@dataclass(frozen=True)
class _ContextBinding:
    runtime: AgentRuntime
    skills: list[tuple[str, str]]
    mcp_servers: list[tuple[str, list[str]]]


def _context_binding(runtime: AgentRuntime) -> _ContextBinding:
    return _ContextBinding(
        runtime,
        runtime.skills_meta(),
        runtime.mcp.server_summary() if runtime.mcp else [],
    )


def _bind_context(ctx: ChatContext, generation: int, binding: _ContextBinding) -> None:
    runtime = binding.runtime
    ctx.config = runtime.config
    ctx.loop = runtime.loop
    ctx.store = runtime.session_store
    ctx.logger = runtime.logger
    ctx.skills = binding.skills
    ctx.mcp_servers = binding.mcp_servers
    ctx.mcp_runtime = runtime.mcp
    ctx.skill_manager = runtime.skill_manager
    ctx.skills_config_store = runtime.skills_config_store
    ctx.mcp_service = runtime.mcp_service
    ctx.tool_context = runtime.tool_context
    ctx.runtime_generation = generation


def _reload_summary(
    target: ReloadTarget,
    generation: int,
    old: RuntimeCapabilities | None,
    new: RuntimeCapabilities,
    skill_report: object | None = None,
) -> str:
    old_skills = {item.name: item.fingerprint for item in old.skills} if old else {}
    new_skills = {item.name: item.fingerprint for item in new.skills}
    old_mcp = _mcp_map(old)
    new_mcp = _mcp_map(new)
    skill_delta = _delta(old_skills, new_skills)
    mcp_delta = _delta(old_mcp, new_mcp)
    lines = [f"Runtime generation {generation} 已刷新（target={target}）。"]
    if target in {"skills", "all"}:
        lines.append(f"Skills：{skill_delta}")
        invalid = tuple(getattr(skill_report, "invalid", ()))
        conflicts = tuple(getattr(skill_report, "conflicts", ()))
        lines.append(
            f"Skill diagnostics：invalid={list(invalid) or '-'} · conflict={list(conflicts) or '-'}"
        )
    if target in {"mcp", "all"}:
        lines.append(f"MCP：{mcp_delta}")
        states = ", ".join(f"{item.name}={item.status}" for item in new.mcp_servers) or "none"
        lines.append(f"MCP states：{states}")
    return "\n".join(lines)


def _mcp_map(capabilities: RuntimeCapabilities | None) -> dict[str, object]:
    if capabilities is None:
        return {}
    return {
        item.name: (item.status, item.tool_names, item.error_category)
        for item in capabilities.mcp_servers
    }


def _delta(old: Mapping[str, object], new: Mapping[str, object]) -> str:
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    updated = sorted(name for name in old.keys() & new.keys() if old[name] != new[name])
    return f"added={added or '-'} · removed={removed or '-'} · updated={updated or '-'}"


def _skill_snapshot(manager: object | None) -> tuple[tuple[str, int, int], ...]:
    root_method = getattr(manager, "root", None)
    if not callable(root_method):
        return ()
    roots = [root_method("project"), root_method("user")]
    workspace_root = getattr(manager, "workspace_root", None)
    if workspace_root is not None:
        roots.extend(legacy_project_skills_dirs(workspace_root))
    entries: list[tuple[str, int, int]] = []
    for root in roots:
        try:
            files = root.rglob("*") if root.is_dir() else ()
            for index, path in enumerate(files):
                if index >= 2000:
                    entries.append((f"{Path(root).resolve()}::<truncated>", 0, index))
                    break
                if not path.is_file():
                    continue
                stat = Path(path).stat()
                entries.append((str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return tuple(sorted(entries))


__all__ = ["CLIRuntimeHolder", "ReloadTarget"]
