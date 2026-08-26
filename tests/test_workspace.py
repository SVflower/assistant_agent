"""M14b Workspace 执行边界。"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from assistant_agent.execution import (
    ConfinedWorkspace,
    HostWorkspace,
    ProcessSupervisor,
    ReadOnlyWorkspace,
    RunControl,
    WorkspaceError,
)
from assistant_agent.tools.file_edit import WriteFileTool
from assistant_agent.tools.file_read import ReadFileTool
from assistant_agent.tools.shell import ShellTool
from tests.support import ToolContextFixture


def _workspace(kind, root):
    return kind(root, supervisor=ProcessSupervisor(), control=RunControl())


def test_host_workspace_allows_outside_but_bases_relative_paths_on_root(tmp_path):
    workspace = _workspace(HostWorkspace, tmp_path / "root")
    outside = tmp_path / "outside.txt"
    assert workspace.resolve_path("inside.txt") == (tmp_path / "root" / "inside.txt").resolve()
    assert workspace.resolve_path(outside) == outside.resolve()


def test_confined_workspace_rejects_relative_and_absolute_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    workspace = _workspace(ConfinedWorkspace, root)
    assert workspace.resolve_path("inside.txt") == (root / "inside.txt").resolve()
    with pytest.raises(WorkspaceError, match="超出受限工作区"):
        workspace.resolve_path("../outside.txt")
    with pytest.raises(WorkspaceError, match="超出受限工作区"):
        workspace.resolve_path(tmp_path / "outside.txt")


def test_confined_workspace_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前平台不能创建测试符号链接：{exc}")
    workspace = _workspace(ConfinedWorkspace, root)
    with pytest.raises(WorkspaceError, match="超出受限工作区"):
        workspace.resolve_path("link/secret.txt")


def test_file_tools_use_workspace_root_and_reject_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    workspace = _workspace(ConfinedWorkspace, root)
    ctx = ToolContextFixture(workspace=workspace)
    written = WriteFileTool().run({"path": "inside.txt", "content": "ok"}, ctx)
    escaped = WriteFileTool().run({"path": "../outside.txt", "content": "bad"}, ctx)
    read = ReadFileTool().run({"path": "inside.txt"}, ctx)
    assert written.code == "ok"
    assert read.output == "ok"
    assert escaped.code == "workspace_escape"
    assert not (tmp_path / "outside.txt").exists()


def test_shell_executes_with_workspace_as_cwd(tmp_path):
    workspace = _workspace(ConfinedWorkspace, tmp_path)
    ctx = ToolContextFixture(workspace=workspace, shell_timeout=10)
    code = "import pathlib; pathlib.Path('cwd-marker.txt').write_text('ok')"
    command = subprocess.list2cmdline([sys.executable, "-c", code])
    result = ShellTool().run({"command": command}, ctx)
    assert result.code == "ok"
    assert (tmp_path / "cwd-marker.txt").read_text(encoding="utf-8") == "ok"


def test_read_only_workspace_rejects_writes_and_processes(tmp_path):
    workspace = _workspace(ReadOnlyWorkspace, tmp_path)
    ctx = ToolContextFixture(workspace=workspace)
    written = WriteFileTool().run({"path": "blocked.txt", "content": "no"}, ctx)
    shell = ShellTool().run({"command": "echo blocked"}, ctx)
    assert written.code == "filesystem_read_only"
    assert shell.code == "filesystem_read_only"
    assert not (tmp_path / "blocked.txt").exists()


def test_workspace_backend_discloses_application_only_boundary(tmp_path):
    workspace = _workspace(ConfinedWorkspace, tmp_path)
    assert workspace.backend == "confined"
    assert workspace.os_sandboxed is False
    assert os.fspath(workspace.root) == os.fspath(tmp_path.resolve())
