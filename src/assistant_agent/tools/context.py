"""工具执行所需的端口化运行上下文。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from assistant_agent.contracts.interactions import InteractionPort
from assistant_agent.tools.interaction_bridge import approve_via_port, ask_via_port
from assistant_agent.tools.models import ArtifactRef, ToolBudget
from assistant_agent.tools.observers import PostToolUseObserver, PreToolUseObserver
from assistant_agent.tools.permissions import (
    PermissionDecision,
    PermissionRequest,
    PermissionScope,
)
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.ports import (
    ArtifactStorePort,
    ManagedProcessRegistryPort,
    ProcessResultPort,
    RunControlPort,
    ToolTelemetry,
    WorkspacePort,
)

# 确认结果：允许一次 / 当前 scope / 上级 scope / 拒绝。
ConfirmChoice = Literal["allow", "always", "broader", "deny"]

# 无人应答时的退化信号：非交互环境（管道/无 tty）下 ask_user 的返回。
NO_USER_AVAILABLE = "当前非交互环境，无用户应答；请基于最合理的假设继续，并说明你做了哪些假设。"


@dataclass
class ToolContext:
    """工具执行时可用的运行时上下文。

    把"是否需要确认危险操作""超时"等设置和确认回调注入工具，
    使工具本身不直接依赖配置或 UI。
    """

    workspace: WorkspacePort
    run_control: RunControlPort
    logger: ToolTelemetry
    artifact_store: ArtifactStorePort
    process_manager: ManagedProcessRegistryPort | None = None
    confirm_dangerous_shell: bool = True
    shell_timeout: int = 60
    # 确认回调：给一条说明，返回用户的选择（allow/always/deny）。
    # 默认拒绝（安全优先）。UI 层注入真正的多选交互。
    confirm: Callable[[str], ConfirmChoice] = lambda _msg: "deny"
    confirm_scoped: Callable[[str, str], ConfirmChoice] | None = None
    # 澄清回调（层1）：给问题+选项，返回用户所选。默认返回退化信号（无 UI 可问）。
    # UI 层注入真正的交互；ask_user 工具在非交互环境会自行退化，不调用它。
    ask: Callable[[str, list[str]], str] = lambda _q, _opts: NO_USER_AVAILABLE
    # 公共服务交互端口。非空时优先于上面的 CLI 兼容回调。
    interaction: InteractionPort | None = None
    sanitize_for_display: Callable[[Any], object] = lambda _value: "[hidden]"
    # 本会话内"永远允许"的类别集合（如 "run_shell"）。由 request_confirm 维护。
    always_allowed: set[str] = field(default_factory=set)
    # M9b：精确会话授权。旧 always_allowed 暂留作兼容，不参与新策略。
    permission_grants: set[PermissionScope] = field(default_factory=set)
    # 工作区根目录：写在此目录树内直接放行，写到外面需确认（默认启动时的 cwd）。
    workspace_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    permission_policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    interactive: bool = True
    pre_tool_observers: list[PreToolUseObserver] = field(default_factory=list)
    post_tool_observers: list[PostToolUseObserver] = field(default_factory=list)
    # 单个工具输出写入上下文的最大字符数（0=不截断）。
    max_output_chars: int = 0
    # 每个进程 stdout/stderr 在内存中保留的最大字符数；超限继续 drain 但丢弃中间内容。
    max_captured_output_chars: int = 1_000_000
    # workspace 内 artifact 最多保留文件数。
    max_artifact_files: int = 100
    artifact_root: Path | None = None
    # 当前任务预算；由 AgentLoop 在每次 run() 开始时安装，结束时恢复。
    budget: ToolBudget | None = None
    # 当前稳定工具调用 ID；仅 Tool.run 执行期间设置，供支持幂等键的扩展工具使用。
    current_call_id: str = ""
    current_run_id: str = ""
    current_session_id: str | None = None
    # 当前工具执行内确认回调的累计等待时间。
    _approval_wait_ms: int = 0

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace.root

    def resolve_path(self, value: str | Path) -> Path:
        return self.workspace.resolve_path(value)

    def execute_process(
        self,
        command: str | list[str],
        *,
        shell: bool,
        timeout: float,
        max_stream_chars: int,
        cwd: str | Path | None = None,
    ) -> ProcessResultPort:
        return self.workspace.execute(
            command,
            shell=shell,
            timeout=timeout,
            max_stream_chars=max_stream_chars,
            cwd=cwd,
        )

    def reset_approval_wait(self) -> None:
        self._approval_wait_ms = 0

    def bind_run(self, run_id: str, session_id: str | None) -> None:
        """绑定当前服务调用身份；每个 Runtime 同时只允许一个活跃 Run。"""
        self.current_run_id = run_id
        self.current_session_id = session_id

    def clear_run(self) -> None:
        self.current_run_id = ""
        self.current_session_id = None

    def request_question(self, question: str, options: list[str]) -> str:
        if self.interaction is None:
            return self.ask(question, options)
        answer = ask_via_port(
            self.interaction,
            run_id=self.current_run_id,
            session_id=self.current_session_id,
            call_id=self.current_call_id,
            question=question,
            options=options,
            sanitize=self.sanitize_for_display,
        )
        return answer if answer is not None else NO_USER_AVAILABLE

    def write_artifact(self, content: str, *, prefix: str, complete: bool = True) -> ArtifactRef:
        return self.artifact_store.write_text(content, prefix=prefix, complete=complete)

    def consume_approval_wait(self) -> int:
        value = self._approval_wait_ms
        self._approval_wait_ms = 0
        return value

    def request_confirm(self, category: str, message: str) -> bool:
        """请求某类危险操作的确认，返回是否放行。

        统一处理"永远允许"记忆：某类别一旦被选为 always，本会话内同类不再询问。
        工具只需调用本方法，不直接接触多选逻辑。授权决策写入审计日志。
        确认回调墙钟耗时会累加，供 registry 从完整耗时中剥离。
        """
        if category in self.always_allowed:
            # 命中永久允许：未真正询问用户，不计等待时间。
            self.logger.confirm(category=category, decision="allow", remembered=True)
            return True
        start = time.perf_counter()
        choice = self.confirm(message)
        self._approval_wait_ms += int((time.perf_counter() - start) * 1000)
        if choice == "always":
            self.always_allowed.add(category)
            self.logger.confirm(category=category, decision="always", remembered=False)
            return True
        self.logger.confirm(category=category, decision=choice, remembered=False)
        return choice == "allow"

    def request_permissions(
        self,
        requests: list[PermissionRequest],
        *,
        before_prompt: Callable[[], None] | None = None,
    ) -> bool:
        """合并权限请求并执行一次确认；deny 优先，ask 在非交互模式下拒绝。"""
        if not requests:
            return True
        decisions = [
            self.permission_policy.decide(
                request,
                workspace_root=self.workspace_root,
                grants=self.permission_grants,
            )
            for request in requests
        ]
        denied = next((decision for decision in decisions if decision.effect == "deny"), None)
        if denied is not None:
            self._audit_permissions(requests, decisions, "deny", denied.reason, denied.remembered)
            return False
        asks = [
            (request, decision)
            for request, decision in zip(requests, decisions, strict=True)
            if decision.effect == "ask"
        ]
        if not asks:
            remembered = bool(decisions) and all(decision.remembered for decision in decisions)
            self._audit_permissions(requests, decisions, "allow", "策略自动允许", remembered)
            return True
        if not self.interactive:
            self._audit_permissions(requests, decisions, "deny", "非交互模式无法请求授权", False)
            return False

        if before_prompt is not None:
            before_prompt()
        lines = ["需要授权："]
        lines.extend(
            f"- {request.capability.value}: {request.display_target}" for request, _decision in asks
        )
        risks = list(dict.fromkeys(request.risk for request, _decision in asks))
        if risks:
            lines.append(f"风险：{'；'.join(risks)}")
        start = time.perf_counter()
        broader = [request.broader_scope for request, _decision in asks]
        can_grant_broader = bool(broader) and all(
            scope is not None and scope == broader[0] for scope in broader
        )
        if self.interaction is not None:
            broader_scope = broader[0] if can_grant_broader else None
            choice = approve_via_port(
                self.interaction,
                run_id=self.current_run_id,
                session_id=self.current_session_id,
                call_id=self.current_call_id,
                asks=[item for item, _decision in asks],
                risks=risks,
                broader_scope=broader_scope,
                broader_scope_label=(
                    str(asks[0][0].metadata.get("broader_scope_label", "本会话允许上级范围"))
                    if can_grant_broader
                    else ""
                ),
                sanitize=self.sanitize_for_display,
            )
        elif can_grant_broader and self.confirm_scoped is not None:
            label = str(asks[0][0].metadata.get("broader_scope_label", "本会话允许上级范围"))
            choice = self.confirm_scoped("\n".join(lines), label)
        else:
            choice = self.confirm("\n".join(lines))
        self._approval_wait_ms += int((time.perf_counter() - start) * 1000)
        if choice == "broader" and can_grant_broader and broader[0] is not None:
            self.permission_grants.add(broader[0])
            self._audit_permissions(
                requests, decisions, "always", "用户允许并记住上级会话作用域", False
            )
            return True
        if choice == "always":
            self.permission_grants.update(request.scope for request, _decision in asks)
            self._audit_permissions(
                requests, decisions, "always", "用户允许并记住精确作用域", False
            )
            return True
        self._audit_permissions(requests, decisions, choice, "用户确认结果", False)
        return choice == "allow"

    def _audit_permissions(
        self,
        requests: list[PermissionRequest],
        decisions: list[PermissionDecision],
        decision: str,
        reason: str,
        remembered: bool,
    ) -> None:
        self.logger.permission_decision(
            mode=self.permission_policy.mode,
            tool=requests[0].tool,
            capabilities=[request.capability.value for request in requests],
            targets=[request.target for request in requests],
            decision=decision,
            reason=reason,
            remembered=remembered,
            matched_rules=[
                f"{item.matched_rule.effect}:{item.matched_rule.capability.value}:"
                f"{item.matched_rule.tool}:{item.matched_rule.target}"
                for item in decisions
                if item.matched_rule is not None
            ],
        )
