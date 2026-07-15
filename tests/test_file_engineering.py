"""M10a 大文件分页、原子写与换行保持。"""

from __future__ import annotations

import os

import pytest

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.file_edit import EditFileTool, MultiEditTool, WriteFileTool
from assistant_agent.tools.file_read import ListDirTool, ReadFileTool
from assistant_agent.tools.registry import ToolRegistry


def _execute(tool, args, tmp_path):
    registry = ToolRegistry()
    registry.register(tool)
    return registry.execute(tool.name, args, ToolContext(workspace_root=tmp_path))


def test_read_100k_lines_middle_page_is_bounded(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("".join(f"line-{index}\n" for index in range(1, 100_001)), encoding="utf-8")
    result = _execute(
        ReadFileTool(),
        {"path": str(path), "start_line": 50_000, "end_line": 50_010},
        tmp_path,
    )
    assert result.code == "ok"
    assert "line-50000" in result.output
    assert "line-49999" not in result.output
    assert result.metadata["total_lines"] == 100_000
    assert result.metadata["has_more"] is True
    assert len(result.output) < 2_000


def test_read_small_file_keeps_legacy_plain_output(tmp_path):
    path = tmp_path / "small.txt"
    path.write_bytes(b"a\r\nb\r\n")
    result = ReadFileTool().run({"path": str(path)}, ToolContext())
    assert result.output == "a\r\nb\r\n"
    assert result.metadata["has_more"] is False


def test_read_empty_file_keeps_default_compatibility_but_rejects_explicit_range(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    default = _execute(ReadFileTool(), {"path": str(path)}, tmp_path)
    explicit = _execute(
        ReadFileTool(), {"path": str(path), "start_line": 1, "end_line": 1}, tmp_path
    )
    assert default.output == ""
    assert default.metadata["total_lines"] == 0
    assert explicit.code == "range_out_of_bounds"


def test_read_rejects_invalid_and_out_of_bounds_ranges(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    invalid = _execute(
        ReadFileTool(), {"path": str(path), "start_line": 5, "end_line": 2}, tmp_path
    )
    outside = _execute(ReadFileTool(), {"path": str(path), "start_line": 5}, tmp_path)
    assert invalid.code == "invalid_arguments"
    assert outside.code == "range_out_of_bounds"


def test_atomic_write_failure_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.txt"
    path.write_text("old", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("injected")

    monkeypatch.setattr("assistant_agent.tools.file_io.os.replace", fail_replace)
    result = WriteFileTool().run({"path": str(path), "content": "new"}, ToolContext())
    assert result.code == "io_error"
    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".state.txt.*.tmp")) == []


def test_edit_preserves_crlf_when_model_uses_lf(tmp_path):
    path = tmp_path / "windows.txt"
    path.write_bytes(b"one\r\ntwo\r\n")
    result = EditFileTool().run(
        {"path": str(path), "old_string": "one\ntwo", "new_string": "ONE\nTWO"},
        ToolContext(),
    )
    assert result.code == "ok"
    assert path.read_bytes() == b"ONE\r\nTWO\r\n"


def test_multi_edit_atomic_replace_failure_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "multi.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    monkeypatch.setattr(
        "assistant_agent.tools.file_io.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("injected")),
    )
    result = MultiEditTool().run(
        {
            "path": str(path),
            "edits": [
                {"old_string": "one", "new_string": "1"},
                {"old_string": "two", "new_string": "2"},
            ],
        },
        ToolContext(),
    )
    assert result.code == "io_error"
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


def test_atomic_edit_preserves_existing_permissions(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows mode bits do not provide the same guarantee")
    path = tmp_path / "mode.txt"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o640)
    result = EditFileTool().run(
        {"path": str(path), "old_string": "old", "new_string": "new"}, ToolContext()
    )
    assert result.code == "ok"
    assert path.stat().st_mode & 0o777 == 0o640


def test_list_dir_is_bounded(tmp_path):
    for index in range(20):
        (tmp_path / f"file-{index:02}.txt").write_text("x", encoding="utf-8")
    result = _execute(ListDirTool(), {"path": str(tmp_path), "max_results": 5}, tmp_path)
    assert result.metadata == {"returned": 5, "truncated": True}
    assert result.output.count("[file]") == 5
