"""工具基类与公共类型。

工具是 Agent 的扩展点：新增能力 = 写一个 Tool 子类并注册，内核循环不变。
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from assistant_agent.obs import NullLogger

# 确认结果：允许一次 / 本会话永远允许这类 / 拒绝。
ConfirmChoice = Literal["allow", "always", "deny"]

# 无人应答时的退化信号：非交互环境（管道/无 tty）下 ask_user 的返回。
NO_USER_AVAILABLE = "当前非交互环境，无用户应答；请基于最合理的假设继续，并说明你做了哪些假设。"


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
    # 工作区根目录：写在此目录树内直接放行，写到外面需确认（默认启动时的 cwd）。
    workspace_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    # 事件日志器（可观测/审计）。默认 NullLogger（零副作用）；main 注入真正的 EventLogger。
    logger: NullLogger = field(default_factory=NullLogger)

    def request_confirm(self, category: str, message: str) -> bool:
        """请求某类危险操作的确认，返回是否放行。

        统一处理"永远允许"记忆：某类别一旦被选为 always，本会话内同类不再询问。
        工具只需调用本方法，不直接接触多选逻辑。授权决策写入审计日志。
        """
        if category in self.always_allowed:
            self.logger.confirm(category=category, decision="allow", remembered=True)
            return True
        choice = self.confirm(message)
        if choice == "always":
            self.always_allowed.add(category)
            self.logger.confirm(category=category, decision="always", remembered=False)
            return True
        self.logger.confirm(category=category, decision=choice, remembered=False)
        return choice == "allow"


@dataclass
class ToolResult:
    """工具执行结果。"""

    output: str
    is_error: bool = False

    @classmethod
    def ok(cls, output: str) -> ToolResult:
        return cls(output=output, is_error=False)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(output=message, is_error=True)


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
