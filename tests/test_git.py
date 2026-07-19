"""git 只读工具测试。"""

from __future__ import annotations

import subprocess

from assistant_agent.tools.git import GitTool
from assistant_agent.tools.permissions import Capability
from tests.support import ToolContextFixture


def _ctx() -> ToolContextFixture:
    return ToolContextFixture(shell_timeout=30)


def _init_repo(tmp_path):
    """在 tmp_path 建一个带一次提交的 git 仓库。"""

    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, stdin=subprocess.DEVNULL)

    run("init")
    run("config", "user.email", "t@t.com")
    run("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-m", "init")
    return tmp_path


def _run_in(tmp_path, args):
    """在 tmp_path 目录下执行 git 工具（切 cwd）。"""
    import os

    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        return GitTool().run(args, _ctx())
    finally:
        os.chdir(old)


def test_git_status(tmp_path):
    _init_repo(tmp_path)
    r = _run_in(tmp_path, {"subcommand": "status"})
    assert not r.is_error
    assert "退出码：0" in r.output


def test_git_log(tmp_path):
    _init_repo(tmp_path)
    r = _run_in(tmp_path, {"subcommand": "log"})
    assert not r.is_error
    assert "init" in r.output


def test_git_diff_after_change(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("hello\nworld\n", encoding="utf-8")
    r = _run_in(tmp_path, {"subcommand": "diff"})
    assert not r.is_error
    assert "world" in r.output


def test_git_write_subcommand_rejected(tmp_path):
    _init_repo(tmp_path)
    for bad in ("commit", "reset", "push", "checkout", "rm"):
        r = _run_in(tmp_path, {"subcommand": bad})
        assert r.is_error, f"{bad} 应被拒绝"
        assert "不支持" in r.output


def test_git_missing_subcommand():
    r = GitTool().run({}, _ctx())
    assert r.is_error


def test_permission_analysis_rejects_write_and_external_diff_flags():
    tool = GitTool()
    ctx = _ctx()
    safe = tool.permission_requests({"subcommand": "status", "args": "--short"}, ctx)
    assert [request.capability for request in safe] == [Capability.PROCESS_EXECUTE]

    for args in ("--output=result.patch", "--ext-diff", "--no-index a b"):
        requests = tool.permission_requests({"subcommand": "diff", "args": args}, ctx)
        assert {request.capability for request in requests} == {
            Capability.PROCESS_EXECUTE,
            Capability.FILESYSTEM_WRITE,
            Capability.NETWORK_ACCESS,
        }


def test_git_non_repo_dir(tmp_path):
    # 非 git 仓库：git 返回非零 + stderr，但工具不当作 error（交模型判断）
    r = _run_in(tmp_path, {"subcommand": "status"})
    assert not r.is_error
    assert "退出码：" in r.output


def test_git_args_no_shell_injection(tmp_path):
    _init_repo(tmp_path)
    # args 经 shlex 解析、shell=False 执行；注入串不会触发 shell
    r = _run_in(tmp_path, {"subcommand": "log", "args": "; echo pwned"})
    # 不应真的执行 echo（不会有 pwned），git 会因非法参数报错或空
    assert "pwned" not in r.output
