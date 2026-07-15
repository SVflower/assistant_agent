"""工具执行结果和 artifact 引用契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArtifactRef:
    """工具产生的受限持久化载荷引用；模型只看到路径和摘要。"""

    id: str
    path: str
    media_type: str = "text/plain"
    size_chars: int = 0
    complete: bool = True


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

    def __post_init__(self) -> None:
        if not self.code:
            self.code = "tool_error" if self.is_error else "ok"

    @classmethod
    def ok(
        cls,
        output: str,
        *,
        code: str = "ok",
        metadata: dict[str, Any] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> ToolResult:
        return cls(
            output=output,
            is_error=False,
            code=code,
            metadata=metadata or {},
            artifacts=artifacts or [],
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
