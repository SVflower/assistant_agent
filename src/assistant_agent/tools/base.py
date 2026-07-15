"""工具基类与公共类型。

工具是 Agent 的扩展点：新增能力 = 写一个 Tool 子类并注册，内核循环不变。
"""

from __future__ import annotations

import abc
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from assistant_agent.obs import NullLogger
from assistant_agent.tools.display import ToolDisplay, call_display, result_display
from assistant_agent.tools.observers import PostToolUseObserver, PreToolUseObserver
from assistant_agent.tools.permissions import (
    Capability,
    PermissionDecision,
    PermissionRequest,
    PermissionScope,
)
from assistant_agent.tools.policy import PermissionPolicy
from assistant_agent.tools.result import ArtifactRef, ToolResult

# 确认结果：允许一次 / 本会话永远允许这类 / 拒绝。
ConfirmChoice = Literal["allow", "always", "deny"]

# 无人应答时的退化信号：非交互环境（管道/无 tty）下 ask_user 的返回。
NO_USER_AVAILABLE = "当前非交互环境，无用户应答；请基于最合理的假设继续，并说明你做了哪些假设。"


@dataclass
class ToolBudget:
    """一次 Agent 任务的工具资源预算。0 表示对应输出上限不启用。"""

    max_calls: int
    max_total_output_chars: int = 0
    used_calls: int = 0
    used_output_chars: int = 0

    def try_consume_call(self) -> str | None:
        """消费一次调用额度；不可执行时返回稳定的耗尽原因。"""
        if self.used_calls >= self.max_calls:
            return "max_tool_calls"
        if (
            self.max_total_output_chars > 0
            and self.used_output_chars >= self.max_total_output_chars
        ):
            return "max_total_tool_output_chars"
        self.used_calls += 1
        return None

    def remaining_output_chars(self) -> int | None:
        """返回累计输出剩余额度；None 表示不限制。"""
        if self.max_total_output_chars == 0:
            return None
        return max(self.max_total_output_chars - self.used_output_chars, 0)

    def consume_output(self, chars: int) -> None:
        self.used_output_chars += max(chars, 0)


@dataclass
class ToolContext:
    """工具执行时可用的运行时上下文。

    把"是否需要确认危险操作""超时"等设置和确认回调注入工具，
    使工具本身不直接依赖配置或 UI。
    """

    confirm_dangerous_shell: bool = True
    shell_timeout: int = 60
    # 确认回调：给一条说明，返回用户的选择（allow/always/deny）。
    # 默认拒绝（安全优先）。UI 层注入真正的多选交互。
    confirm: Callable[[str], ConfirmChoice] = lambda _msg: "deny"
    # 澄清回调（层1）：给问题+选项，返回用户所选。默认返回退化信号（无 UI 可问）。
    # UI 层注入真正的交互；ask_user 工具在非交互环境会自行退化，不调用它。
    ask: Callable[[str, list[str]], str] = lambda _q, _opts: NO_USER_AVAILABLE
    # 本会话内"永远允许"的类别集合（如 "run_shell"）。由 request_confirm 维护。
    always_allowed: set[str] = field(default_factory=set)
    # M9b：精确会话授权。旧 always_allowed 暂留作兼容，不参与新策略。
    permission_grants: set[PermissionScope] = field(default_factory=set)
    # 工作区根目录：写在此目录树内直接放行，写到外面需确认（默认启动时的 cwd）。
    workspace_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    # 事件日志器（可观测/审计）。默认 NullLogger（零副作用）；main 注入真正的 EventLogger。
    logger: NullLogger = field(default_factory=NullLogger)
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
    artifact_store: Any | None = None
    # 当前任务预算；由 AgentLoop 在每次 run() 开始时安装，结束时恢复。
    budget: ToolBudget | None = None
    # 当前稳定工具调用 ID；仅 Tool.run 执行期间设置，供支持幂等键的扩展工具使用。
    current_call_id: str = ""
    # 当前工具执行内确认回调的累计等待时间。
    _approval_wait_ms: int = 0

    def reset_approval_wait(self) -> None:
        self._approval_wait_ms = 0

    def write_artifact(self, content: str, *, prefix: str, complete: bool = True) -> ArtifactRef:
        if self.artifact_store is None:
            from assistant_agent.tools.artifacts import ArtifactStore

            self.artifact_store = ArtifactStore(
                self.workspace_root,
                max_chars=self.max_captured_output_chars,
                max_files=self.max_artifact_files,
            )
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
        lines = ["即将执行需要授权的工具动作："]
        lines.extend(
            f"- {request.capability.value}: {request.target}\n  风险：{request.risk}"
            for request, _decision in asks
        )
        start = time.perf_counter()
        choice = self.confirm("\n".join(lines))
        self._approval_wait_ms += int((time.perf_counter() - start) * 1000)
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


class Tool(abc.ABC):
    """所有工具的基类。"""

    #: 工具名，模型用它来调用。须唯一、稳定。
    name: str = ""
    #: 给模型看的描述，决定模型何时调用。要清晰，对"笨模型"也友好。
    description: str = ""

    @property
    @abc.abstractmethod
    def parameters(self) -> dict[str, Any]:
        """返回 JSON Schema 形式的参数定义（OpenAI function 的 parameters 部分）。"""

    @abc.abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。实现应自行处理异常并返回 ToolResult，不要向外抛。"""

    def to_schema(self) -> dict[str, Any]:
        """转成 OpenAI function-calling 的 tool schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def display_call(self, args: dict[str, Any]) -> ToolDisplay:
        """返回 UI 无关的调用摘要；扩展工具可覆盖。"""
        return call_display(self.name, args)

    def display_result(self, args: dict[str, Any], result: ToolResult) -> ToolDisplay:
        """返回 UI 无关的结果摘要；不改变回喂模型的 ToolResult。"""
        return result_display(self.name, args, result, self.display_call(args))

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        """执行前声明能力。未知扩展默认要求进程级授权，不能因漏声明而放行。"""
        return [
            PermissionRequest(
                tool=self.name,
                capability=Capability.PROCESS_EXECUTE,
                target=self.name or "unknown",
                risk="未知扩展工具可能产生外部副作用",
            )
        ]
