"""M10a code_search 上下文和流式大文件行为。"""

from __future__ import annotations

from pathlib import Path

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.search import CodeSearchTool


def _search(args):
    registry = ToolRegistry()
    registry.register(CodeSearchTool())
    requested = Path(args.get("path") or ".")
    workspace = requested if requested.is_dir() else requested.parent
    return registry.execute("code_search", args, ToolContext(workspace_root=workspace.resolve()))


def test_context_lines_include_neighbors_and_mark_match(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("one\ntwo\nTARGET\nfour\nfive\n", encoding="utf-8")
    result = _search({"pattern": "TARGET", "path": str(path), "context_lines": 1})
    assert "  2: two" in result.output
    assert "> 3: TARGET" in result.output
    assert "  4: four" in result.output
    assert "one" not in result.output
    assert result.metadata["matches"] == 1


def test_overlapping_context_blocks_are_merged(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("a\nMATCH one\nmid\nMATCH two\nz\n", encoding="utf-8")
    result = _search({"pattern": "MATCH", "path": str(path), "context_lines": 1})
    assert result.output.count("sample.txt:") == 1
    assert result.output.count("> ") == 2


def test_search_streams_file_larger_than_old_two_mb_limit(tmp_path):
    path = tmp_path / "large.txt"
    with path.open("w", encoding="utf-8") as handle:
        for index in range(120_000):
            handle.write(f"line {index}\n")
        handle.write("UNIQUE_NEEDLE\n")
    assert path.stat().st_size > 1_000_000
    result = _search({"pattern": "UNIQUE_NEEDLE", "path": str(path)})
    assert "UNIQUE_NEEDLE" in result.output
    assert result.metadata["matches"] == 1


def test_search_schema_rejects_excessive_context_before_run(tmp_path):
    result = _search({"pattern": "x", "path": str(tmp_path), "context_lines": 11})
    assert result.code == "invalid_arguments"
    assert result.executed is False


def test_max_results_only_marks_truncated_when_extra_match_exists(tmp_path):
    path = tmp_path / "matches.txt"
    path.write_text("hit\nhit\n", encoding="utf-8")
    exact = _search({"pattern": "hit", "path": str(path), "max_results": 2})
    overflow = _search({"pattern": "hit", "path": str(path), "max_results": 1})
    assert exact.metadata["truncated"] is False
    assert overflow.metadata["truncated"] is True
