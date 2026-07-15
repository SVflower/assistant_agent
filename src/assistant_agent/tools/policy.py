"""确定性的工具权限策略：显式规则优先，模式提供安全默认值。"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import Path

from assistant_agent.tools.permissions import (
    Capability,
    PermissionDecision,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
)


class PermissionPolicy:
    """对单项 PermissionRequest 做 deny -> ask -> allow 决策。"""

    def __init__(
        self,
        mode: PermissionMode = "workspace",
        rules: list[PermissionRule] | None = None,
        sensitive_paths: Sequence[str | Path] | None = None,
    ) -> None:
        self.mode = mode
        self.rules = list(rules or [])
        defaults = [Path.home() / ".ssh", Path.home() / ".aws", Path.home() / ".gnupg"]
        roots = [*defaults, *(sensitive_paths or [])]
        self.sensitive_paths = list(
            dict.fromkeys(Path(path).expanduser().resolve() for path in roots)
        )

    def decide(
        self,
        request: PermissionRequest,
        *,
        workspace_root: Path,
        grants: set[PermissionScope],
    ) -> PermissionDecision:
        if self._is_sensitive(request):
            return PermissionDecision("deny", "目标位于默认敏感目录")

        matches = [rule for rule in self.rules if self._matches(rule, request)]
        for effect in ("deny", "ask"):
            matched = next((rule for rule in matches if rule.effect == effect), None)
            if matched is not None:
                return PermissionDecision(effect, f"命中显式 {effect} 规则", matched)

        if request.scope in grants or (
            request.broader_scope is not None and request.broader_scope in grants
        ):
            return PermissionDecision("allow", "命中本会话精确授权", remembered=True)

        matched_allow = next((rule for rule in matches if rule.effect == "allow"), None)
        if matched_allow is not None:
            return PermissionDecision("allow", "命中显式 allow 规则", matched_allow)

        return self._mode_default(request, workspace_root.resolve())

    @staticmethod
    def _matches(rule: PermissionRule, request: PermissionRequest) -> bool:
        return (
            rule.capability == request.capability
            and fnmatch.fnmatchcase(request.tool, rule.tool)
            and fnmatch.fnmatchcase(request.target, rule.target)
        )

    def _is_sensitive(self, request: PermissionRequest) -> bool:
        if request.capability not in (Capability.FILESYSTEM_READ, Capability.FILESYSTEM_WRITE):
            return False
        try:
            target = Path(request.target).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return True
        return any(target == root or root in target.parents for root in self.sensitive_paths)

    def _mode_default(self, request: PermissionRequest, workspace_root: Path) -> PermissionDecision:
        capability = request.capability
        inside = self._inside_workspace(request, workspace_root)

        if self.mode == "unrestricted":
            return PermissionDecision("allow", "unrestricted 模式默认允许")

        if capability == Capability.USER_INTERACTION:
            return PermissionDecision("allow", "用户交互工具默认允许")

        if self.mode == "readonly":
            if capability == Capability.FILESYSTEM_READ and inside:
                return PermissionDecision("allow", "readonly 允许工作区内读取")
            if capability == Capability.PROCESS_EXECUTE and request.metadata.get(
                "trusted_readonly"
            ):
                return PermissionDecision("allow", "readonly 允许可信内置只读进程")
            return PermissionDecision("deny", "readonly 模式拒绝非只读能力")

        if self.mode == "strict":
            if capability == Capability.FILESYSTEM_READ and inside:
                return PermissionDecision("allow", "strict 允许工作区内读取")
            return PermissionDecision("ask", "strict 模式要求逐次确认")

        if capability in (Capability.FILESYSTEM_READ, Capability.FILESYSTEM_WRITE):
            return PermissionDecision(
                "allow" if inside else "ask",
                "workspace 内目标" if inside else "目标位于 workspace 外",
            )
        if capability == Capability.PROCESS_EXECUTE and request.metadata.get("trusted_readonly"):
            return PermissionDecision("allow", "可信内置只读进程")
        if capability == Capability.MCP_CALL and request.metadata.get("trusted_server"):
            return PermissionDecision("allow", "配置已信任整个 MCP server")
        if capability == Capability.SKILL_LOAD and (
            request.metadata.get("source") == "personal" or request.metadata.get("trusted")
        ):
            return PermissionDecision("allow", "Skill 来源已受信")
        return PermissionDecision("ask", "workspace 模式要求确认该能力")

    @staticmethod
    def _inside_workspace(request: PermissionRequest, workspace_root: Path) -> bool:
        if request.capability not in (Capability.FILESYSTEM_READ, Capability.FILESYSTEM_WRITE):
            return False
        try:
            target = Path(request.target).expanduser().resolve()
            return target == workspace_root or workspace_root in target.parents
        except (OSError, RuntimeError, ValueError):
            return False
