"""Shell 严格只读集合：无法证明安全的命令必须保守声明广泛能力。"""

from assistant_agent.tools.permissions import Capability
from assistant_agent.tools.shell_policy import (
    is_strict_readonly_command,
    shell_permission_requests,
)


def test_small_readonly_allowlist():
    assert is_strict_readonly_command("git status --short")
    assert is_strict_readonly_command("git log --oneline -n5")
    assert is_strict_readonly_command("python --version")
    assert is_strict_readonly_command("ls -la")


def test_interpreters_scripts_and_installers_are_not_readonly():
    commands = [
        "python -c \"open('x', 'w').write('x')\"",
        "powershell -Command Get-Date",
        "cmd /c dir",
        "./script.sh",
        "pytest --collect-only",
        "curl https://example.com",
        "pip install package",
    ]
    assert all(not is_strict_readonly_command(command) for command in commands)


def test_shell_composition_and_expansion_are_not_readonly():
    commands = [
        "git status | more",
        "git status > out",
        "git status && whoami",
        "echo $HOME",
        "ls ../outside",
        "dir C:\\Users",
    ]
    assert all(not is_strict_readonly_command(command) for command in commands)


def test_unproven_command_declares_conservative_capability_upper_bound():
    requests = shell_permission_requests("curl https://example.com")
    assert {request.capability for request in requests} == {
        Capability.PROCESS_EXECUTE,
        Capability.FILESYSTEM_WRITE,
        Capability.NETWORK_ACCESS,
    }
