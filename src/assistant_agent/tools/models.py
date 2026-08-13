"""工具执行结果和 artifact 引用契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assistant_agent.contracts.charts import ChartArtifactV2
from assistant_agent.contracts.outputs import OutputArtifactV1


@dataclass
class ArtifactRef:
    """工具产生的受限持久化载荷引用；模型只看到路径和摘要。"""

    id: str
    path: str
    media_type: str = "text/plain"
    size_chars: int = 0
    complete: bool = True


@dataclass
class ToolBudget:
    """一次 Agent 任务的工具资源预算。0 表示对应输出上限不启用。"""

    max_calls: int
    max_total_output_chars: int = 0
    used_calls: int = 0
    used_output_chars: int = 0

    def try_consume_call(self) -> str | None:
        if self.used_calls >= self.max_calls:
            return "max_tool_calls"
        if (
            self.max_total_output_chars > 0
            and self.used_output_chars >= self.max_total_output_chars
        ):
            return "max_total_tool_output_chars"
        self.used_calls += 1
        return None

    def exhausted_reason(self) -> str | None:
        if self.used_calls >= self.max_calls:
            return "max_tool_calls"
        if (
            self.max_total_output_chars > 0
            and self.used_output_chars >= self.max_total_output_chars
        ):
            return "max_total_tool_output_chars"
        return None

    def remaining_output_chars(self) -> int | None:
        if self.max_total_output_chars == 0:
            return None
        return max(self.max_total_output_chars - self.used_output_chars, 0)

    def consume_output(self, chars: int) -> None:
        self.used_output_chars += max(chars, 0)


@dataclass
class ToolResult:
    """工具执行结果；output/is_error 保持兼容，其他字段供框架稳定判断。"""

    output: str
    is_error: bool = False
    code: str = ""
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    budget_exhausted: str | None = None
    executed: bool = True
    chart: ChartArtifactV2 | None = None
    output_artifact: OutputArtifactV1 | None = None

    def __post_init__(self) -> None:
        if not self.code:
            self.code = "tool_error" if self.is_error else "ok"
        if self.is_error:
            self.chart = None
            self.output_artifact = None

    @classmethod
    def ok(
        cls,
        output: str,
        *,
        code: str = "ok",
        metadata: dict[str, Any] | None = None,
        artifacts: list[ArtifactRef] | None = None,
        chart: ChartArtifactV2 | None = None,
        output_artifact: OutputArtifactV1 | None = None,
    ) -> ToolResult:
        return cls(
            output=output,
            is_error=False,
            code=code,
            metadata=metadata or {},
            artifacts=artifacts or [],
            chart=chart,
            output_artifact=output_artifact,
        )

    @classmethod
    def error(
        cls,
        message: str,
        *,
        code: str = "tool_error",
        retryable: bool = False,
        metadata: dict[str, Any] | None = None,
        executed: bool = True,
    ) -> ToolResult:
        return cls(
            output=message,
            is_error=True,
            code=code,
            retryable=retryable,
            metadata=metadata or {},
            executed=executed,
        )
