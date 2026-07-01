"""工具系统测试。"""

from __future__ import annotations

from assistant_agent.tools.base import ToolContext, ToolResult
from assistant_agent.tools.file_ops import ListDirTool, ReadFileTool, WriteFileTool
from assistant_agent.tools.registry import build_default_registry
from assistant_agent.tools.shell import ShellTool, _decode, is_dangerous


def _ctx(**kwargs) -> ToolContext:
    return ToolContext(**kwargs)


# ---- file_ops ----


def test_write_then_read(tmp_path):
    target = tmp_path / "sub" / "note.txt"
    write = WriteFileTool().run({"path": str(target), "content": "你好"}, _ctx())
    assert not write.is_error
    assert target.read_text(encoding="utf-8") == "你好"

    read = ReadFileTool().run({"path": str(target)}, _ctx())
    assert not read.is_error
    assert read.output == "你好"


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


# ---- shell ----


def test_is_dangerous():
    assert is_dangerous("rm -rf /tmp/x")
    assert is_dangerous("git push --force")
    assert is_dangerous("echo hi > file.txt")
    assert not is_dangerous("ls -la")
    assert not is_dangerous("echo hi >> file.txt")  # 追加不算危险
    assert not is_dangerous("pytest")


def test_shell_runs_safe_command():
    result = ShellTool().run({"command": "echo hello"}, _ctx())
    assert not result.is_error
    assert "hello" in result.output


def test_shell_dangerous_denied_by_default():
    # 默认 confirm 回调返回 False（拒绝）
    result = ShellTool().run(
        {"command": "rm -rf /tmp/whatever"},
        _ctx(confirm_dangerous_shell=True),
    )
    assert result.is_error
    assert "拒绝" in result.output


def test_shell_dangerous_allowed_when_confirmed(tmp_path):
    target = tmp_path / "to_delete.txt"
    target.write_text("x", encoding="utf-8")
    result = ShellTool().run(
        {"command": f"rm {target}"},
        _ctx(confirm_dangerous_shell=True, confirm=lambda _msg: True),
    )
    assert not result.is_error
    assert not target.exists()


def test_shell_confirm_disabled_runs_dangerous(tmp_path):
    target = tmp_path / "del.txt"
    target.write_text("x", encoding="utf-8")
    result = ShellTool().run(
        {"command": f"rm {target}"},
        _ctx(confirm_dangerous_shell=False),
    )
    assert not result.is_error


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


def test_default_registry_has_four_tools():
    registry = build_default_registry()
    names = set(registry.names())
    assert names == {"read_file", "write_file", "list_dir", "run_shell"}


def test_registry_schemas_shape():
    registry = build_default_registry()
    schemas = registry.schemas()
    assert len(schemas) == 4
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
