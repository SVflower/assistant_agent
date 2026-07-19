"""工具系统测试。"""

from __future__ import annotations

import os
import shlex

from assistant_agent.contracts.capabilities import MCPServerCapability
from assistant_agent.execution.process import _decode
from assistant_agent.tools.file_edit import EditFileTool, MultiEditTool, WriteFileTool
from assistant_agent.tools.file_read import ListDirTool, ReadFileTool
from assistant_agent.tools.registry import ToolRegistry, build_default_registry
from assistant_agent.tools.runtime_inspection import InspectRuntimeTool
from assistant_agent.tools.shell import ShellTool, is_dangerous
from tests.support import ToolContextFixture, ToolResult


def _ctx(**kwargs) -> ToolContextFixture:
    return ToolContextFixture(**kwargs)


def _delete_command(path) -> str:
    if os.name == "nt":
        return f'del /Q "{path}"'
    return f"rm -f {shlex.quote(str(path))}"


def _execute(tool, args, ctx):
    registry = ToolRegistry()
    registry.register(tool)
    return registry.execute(tool.name, args, ctx)


def test_runtime_inspection_uses_live_capabilities_without_permission():
    state = {"status": "discovering"}
    tool = InspectRuntimeTool(
        sandbox="workspace",
        tool_names=lambda: ["read_file", "inspect_runtime"],
        skills=lambda: [("anysearch", "personal")],
        mcp_servers=lambda: [
            MCPServerCapability(
                name="playwright",
                transport="stdio",
                startup="optional",
                status=state["status"],  # type: ignore[arg-type]
            )
        ],
    )

    first = tool.run({}, _ctx())
    state["status"] = "restart_required"
    second = tool.run({}, _ctx())

    assert "playwright" in first.output and "discovering" in first.output
    assert "restart_required" in second.output
    assert "anysearch（personal）" in second.output
    assert "inspect_runtime" in second.output
    assert tool.permission_requests({}, _ctx()) == []


# ---- file_ops ----


def test_write_then_read(tmp_path):
    # 工作区设为 tmp_path，写在区内 → 直接放行
    target = tmp_path / "sub" / "note.txt"
    write = WriteFileTool().run(
        {"path": str(target), "content": "你好"}, _ctx(workspace_root=tmp_path)
    )
    assert not write.is_error
    assert target.read_text(encoding="utf-8") == "你好"

    read = ReadFileTool().run({"path": str(target)}, _ctx())
    assert not read.is_error
    assert read.output == "你好"


def test_write_inside_workspace_no_confirm(tmp_path):
    """区内写不触发确认（confirm 回调即便拒绝也不该被调用）。"""
    called = {"n": 0}

    def confirm(_msg: str) -> str:
        called["n"] += 1
        return "deny"

    target = tmp_path / "a.txt"
    r = WriteFileTool().run(
        {"path": str(target), "content": "x"},
        _ctx(workspace_root=tmp_path, confirm=confirm),
    )
    assert not r.is_error
    assert called["n"] == 0  # 区内写没问


def test_write_outside_workspace_confirmed(tmp_path):
    """区外写：确认放行则写入。"""
    ws = tmp_path / "proj"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    r = _execute(
        WriteFileTool(),
        {"path": str(outside), "content": "x"},
        _ctx(workspace_root=ws, confirm=lambda _m: "allow"),
    )
    assert not r.is_error
    assert outside.read_text(encoding="utf-8") == "x"


def test_write_outside_workspace_denied(tmp_path):
    """区外写：默认拒绝则不写。"""
    ws = tmp_path / "proj"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    r = _execute(
        WriteFileTool(),
        {"path": str(outside), "content": "x"},
        _ctx(workspace_root=ws),  # 默认 confirm 返回 deny
    )
    assert r.is_error
    assert "权限拒绝" in r.output
    assert not outside.exists()


def test_read_missing_file(tmp_path):
    result = ReadFileTool().run({"path": str(tmp_path / "nope.txt")}, _ctx())
    assert result.is_error
    assert "不存在" in result.output


def test_list_dir(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    result = ListDirTool().run({"path": str(tmp_path)}, _ctx())
    assert not result.is_error
    assert "a.txt" in result.output
    assert "subdir" in result.output


# ---- edit_file ----


def test_edit_unique_replace(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    r = EditFileTool().run(
        {"path": str(f), "old_string": "y = 2", "new_string": "y = 3"},
        _ctx(workspace_root=tmp_path),
    )
    assert not r.is_error
    assert f.read_text(encoding="utf-8") == "x = 1\ny = 3\n"


def test_edit_not_found(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    r = EditFileTool().run(
        {"path": str(f), "old_string": "zzz", "new_string": "q"},
        _ctx(workspace_root=tmp_path),
    )
    assert r.is_error
    assert "未找到" in r.output


def test_edit_ambiguous_multiple(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("a\na\n", encoding="utf-8")
    r = EditFileTool().run(
        {"path": str(f), "old_string": "a", "new_string": "b"},
        _ctx(workspace_root=tmp_path),
    )
    assert r.is_error
    assert "歧义" in r.output
    # 未改动
    assert f.read_text(encoding="utf-8") == "a\na\n"


def test_edit_replace_all(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("a\na\n", encoding="utf-8")
    r = EditFileTool().run(
        {"path": str(f), "old_string": "a", "new_string": "b", "replace_all": True},
        _ctx(workspace_root=tmp_path),
    )
    assert not r.is_error
    assert f.read_text(encoding="utf-8") == "b\nb\n"


def test_edit_missing_file(tmp_path):
    r = EditFileTool().run(
        {"path": str(tmp_path / "nope.py"), "old_string": "a", "new_string": "b"},
        _ctx(workspace_root=tmp_path),
    )
    assert r.is_error
    assert "不存在" in r.output


def test_edit_outside_workspace_denied(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    outside = tmp_path / "o.py"
    outside.write_text("a\n", encoding="utf-8")
    r = _execute(
        EditFileTool(),
        {"path": str(outside), "old_string": "a", "new_string": "b"},
        _ctx(workspace_root=ws),  # 默认 deny
    )
    assert r.is_error
    assert "权限拒绝" in r.output
    assert outside.read_text(encoding="utf-8") == "a\n"  # 未改


# ---- multi_edit ----


def test_multi_edit_sequential(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    r = MultiEditTool().run(
        {
            "path": str(f),
            "edits": [
                {"old_string": "one", "new_string": "1"},
                {"old_string": "three", "new_string": "3"},
            ],
        },
        _ctx(workspace_root=tmp_path),
    )
    assert not r.is_error
    assert f.read_text(encoding="utf-8") == "1\ntwo\n3\n"


def test_multi_edit_atomic_abort(tmp_path):
    """任一处失败 → 整体不改（原子）。"""
    f = tmp_path / "a.py"
    f.write_text("one\ntwo\n", encoding="utf-8")
    r = MultiEditTool().run(
        {
            "path": str(f),
            "edits": [
                {"old_string": "one", "new_string": "1"},
                {"old_string": "zzz", "new_string": "q"},  # 第二处找不到
            ],
        },
        _ctx(workspace_root=tmp_path),
    )
    assert r.is_error
    assert "中止" in r.output
    # 第一处也不应生效（原子）
    assert f.read_text(encoding="utf-8") == "one\ntwo\n"


# ---- shell ----


def test_is_dangerous():
    assert is_dangerous("rm -rf /tmp/x")
    assert is_dangerous("git push --force")
    assert is_dangerous("echo hi > file.txt")
    assert not is_dangerous("ls -la")
    assert is_dangerous("echo hi >> file.txt")
    assert is_dangerous("pytest")


def test_shell_runs_safe_command():
    result = ShellTool().run({"command": "echo hello"}, _ctx())
    assert not result.is_error
    assert "hello" in result.output


def test_shell_dangerous_denied_by_default():
    # 默认 confirm 回调返回 False（拒绝）
    result = _execute(
        ShellTool(),
        {"command": "rm -rf /tmp/whatever"},
        _ctx(confirm_dangerous_shell=True),
    )
    assert result.is_error
    assert "拒绝" in result.output


def test_shell_dangerous_allowed_when_confirmed(tmp_path):
    target = tmp_path / "to_delete.txt"
    target.write_text("x", encoding="utf-8")
    result = _execute(
        ShellTool(),
        {"command": _delete_command(target)},
        _ctx(confirm_dangerous_shell=True, confirm=lambda _msg: "allow"),
    )
    assert not result.is_error
    assert not target.exists()


def test_shell_always_allow_is_exact_command_scoped(tmp_path):
    """永久允许只覆盖精确命令，不扩散到同类但不同目标。"""
    calls = {"n": 0}

    def confirm(_msg: str) -> str:
        calls["n"] += 1
        return "always"

    ctx = _ctx(confirm_dangerous_shell=True, confirm=confirm)
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("x", encoding="utf-8")
    f2.write_text("x", encoding="utf-8")
    _execute(ShellTool(), {"command": _delete_command(f1)}, ctx)
    _execute(ShellTool(), {"command": _delete_command(f2)}, ctx)
    assert calls["n"] == 2
    assert not f1.exists()
    assert not f2.exists()
    assert ctx.permission_grants


def test_legacy_shell_confirm_flag_cannot_disable_permission_boundary(tmp_path):
    target = tmp_path / "del.txt"
    target.write_text("x", encoding="utf-8")
    result = _execute(
        ShellTool(),
        {"command": _delete_command(target)},
        _ctx(confirm_dangerous_shell=False),
    )
    assert result.is_error
    assert target.exists()


def test_shell_interactive_command_does_not_hang():
    """交互式命令因 stdin 被切断而立即返回，不会卡到超时。

    回归测试：修复前 `date`（Windows cmd）等交互命令会阻塞到超时。
    这里给一个很短的超时，若 stdin 未被切断会命中超时报错。
    """
    import sys as _sys

    cmd = "date" if _sys.platform == "win32" else "cat"
    result = ShellTool().run({"command": cmd}, _ctx(shell_timeout=5))
    # 不管命令成功与否，关键是没有超时（超时信息里含"超时"字样）
    assert "超时" not in result.output


def test_decode_handles_gbk_bytes():
    """GBK 字节能被容错解码，不崩、不返回乱码占位。"""
    text = "当前日期"
    result = _decode(text.encode("gbk"))
    # Windows 上应还原为中文；非 Windows 至少不崩且返回字符串
    assert isinstance(result, str)
    assert result  # 非空


def test_decode_empty():
    assert _decode(None) == ""
    assert _decode(b"") == ""


# ---- registry ----


def test_default_registry_has_expected_tools():
    registry = build_default_registry()
    names = set(registry.names())
    assert names == {
        "read_file",
        "write_file",
        "edit_file",
        "multi_edit",
        "list_dir",
        "run_shell",
        "code_search",
        "git",
        "ask_user",
    }


def test_registry_schemas_shape():
    registry = build_default_registry()
    schemas = registry.schemas()
    assert len(schemas) == 9
    for schema in schemas:
        assert schema["type"] == "function"
        assert "name" in schema["function"]
        assert "parameters" in schema["function"]


def test_registry_unknown_tool():
    registry = build_default_registry()
    result = registry.execute("nonexistent", {}, _ctx())
    assert result.is_error
    assert "未知工具" in result.output


def test_registry_catches_tool_exception():
    registry = build_default_registry()
    # read_file 传入错误类型，确保异常被兜底为 ToolResult
    result = registry.execute("read_file", {"path": 123}, _ctx())
    assert isinstance(result, ToolResult)
    assert result.is_error
