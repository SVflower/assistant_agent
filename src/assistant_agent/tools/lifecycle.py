"""工具执行生命周期协议；上层可在副作用边界持久化状态。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.permissions import PermissionRequest

ReplayPolicy = Literal["safe_readonly", "safe_idempotent", "requires_decision"]


class ToolExecutionLifecycle(Protocol):
    def approval_pending(
        self,
        call_id: str,
        requests: Sequence[PermissionRequest],
        replay_policy: ReplayPolicy,
    ) -> None: ...

    def tool_started(
        self,
        call_id: str,
        requests: Sequence[PermissionRequest],
        replay_policy: ReplayPolicy,
    ) -> None: ...

    def tool_completed(
        self,
        call_id: str,
        result: ToolResult,
        requests: Sequence[PermissionRequest],
        replay_policy: ReplayPolicy,
    ) -> None: ...
