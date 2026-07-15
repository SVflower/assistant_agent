"""Shell 权限分析：只证明极小安全集合，其余按广泛副作用处理。"""

from __future__ import annotations

import re
import shlex

from assistant_agent.tools.permissions import Capability, PermissionRequest

_UNSAFE_SYNTAX = re.compile(r"[\r\n;&|<>`$(){}\[\]%!]")
_SAFE_BUILTINS = {"pwd", "ver", "ls", "dir"}
_VERSION_COMMANDS = {"python", "python3", "py"}
_GIT_STATUS_FLAGS = {"--short", "--porcelain", "--branch", "--show-stash", "-s", "-b"}
_GIT_DIFF_FLAGS = {"--stat", "--name-only", "--name-status", "--cached", "--staged", "--check"}
_GIT_LOG_FLAGS = {"--oneline", "--decorate", "--stat"}


def _tokens(command: str) -> list[str] | None:
    if _UNSAFE_SYNTAX.search(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    return tokens or None


def is_strict_readonly_command(command: str) -> bool:
    """仅认可无展开/组合/重定向的少量 shell 内建查询。"""
    tokens = _tokens(command.strip())
    if not tokens:
        return False
    executable = tokens[0].lower()
    if executable == "ls":
        return all(token.startswith("-") for token in tokens[1:])
    if executable in _SAFE_BUILTINS:
        return len(tokens) == 1
    if executable in _VERSION_COMMANDS:
        return len(tokens) == 2 and tokens[1].lower() in {"--version", "-v"}
    if executable != "git" or len(tokens) < 2:
        return False
    subcommand = tokens[1].lower()
    args = tokens[2:]
    if subcommand == "status":
        return all(arg in _GIT_STATUS_FLAGS for arg in args)
    if subcommand == "diff":
        return all(arg in _GIT_DIFF_FLAGS for arg in args)
    if subcommand in {"log", "show"}:
        return all(_safe_git_history_arg(arg) for arg in args)
    return False


def _safe_git_history_arg(arg: str) -> bool:
    if arg in _GIT_LOG_FLAGS:
        return True
    if re.fullmatch(r"-n\d+", arg) or re.fullmatch(r"--max-count=\d+", arg):
        return True
    return not arg.startswith("-")


def shell_permission_requests(command: str, tool: str = "run_shell") -> list[PermissionRequest]:
    normalized = " ".join(command.strip().split()) or "<empty>"
    if is_strict_readonly_command(command):
        return [
            PermissionRequest(
                tool=tool,
                capability=Capability.PROCESS_EXECUTE,
                target=normalized,
                risk="受限只读 shell 内建命令",
                metadata={"trusted_readonly": True},
            )
        ]
    risk = "任意进程可能执行代码、修改文件或访问网络；当前无 OS 级沙箱"
    return [
        PermissionRequest(tool, Capability.PROCESS_EXECUTE, normalized, risk),
        PermissionRequest(tool, Capability.FILESYSTEM_WRITE, normalized, risk),
        PermissionRequest(tool, Capability.NETWORK_ACCESS, "unknown", risk),
    ]
