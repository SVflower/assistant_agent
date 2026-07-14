"""code_search（grep）工具测试。"""

from __future__ import annotations

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.search import CodeSearchTool


def _ctx() -> ToolContext:
    return ToolContext()


def _make_tree(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    return 'Hello World'\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hello there\nfoo bar\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("x = 1\ndef hello_again():\n    pass\n", encoding="utf-8")
    # 忽略目录里的文件不应被搜到
    ignored = tmp_path / ".venv"
    ignored.mkdir()
    (ignored / "d.py").write_text("def hello_ignored(): pass\n", encoding="utf-8")
    return tmp_path


def test_search_finds_matches(tmp_path):
    _make_tree(tmp_path)
    r = CodeSearchTool().run({"pattern": "def hello", "path": str(tmp_path)}, _ctx())
    assert not r.is_error
    assert "a.py" in r.output
    assert "c.py" in r.output.replace("\\", "/")  # 跨平台路径分隔


def test_search_glob_filter(tmp_path):
    _make_tree(tmp_path)
    r = CodeSearchTool().run({"pattern": "hello", "path": str(tmp_path), "glob": "*.py"}, _ctx())
    # 只搜 .py，b.txt 的 "hello there" 不应出现
    assert "b.txt" not in r.output


def test_search_ignore_case(tmp_path):
    _make_tree(tmp_path)
    r = CodeSearchTool().run(
        {"pattern": "HELLO WORLD", "path": str(tmp_path), "ignore_case": True}, _ctx()
    )
    assert "a.py" in r.output


def test_search_skips_ignored_dirs(tmp_path):
    _make_tree(tmp_path)
    r = CodeSearchTool().run({"pattern": "hello_ignored", "path": str(tmp_path)}, _ctx())
    assert r.output == "未找到匹配"


def test_search_no_match(tmp_path):
    _make_tree(tmp_path)
    r = CodeSearchTool().run({"pattern": "zzz_not_present", "path": str(tmp_path)}, _ctx())
    assert r.output == "未找到匹配"


def test_search_max_results_truncates(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("\n".join("match" for _ in range(50)), encoding="utf-8")
    r = CodeSearchTool().run({"pattern": "match", "path": str(tmp_path), "max_results": 5}, _ctx())
    assert "已截断" in r.output
    # 5 条匹配 + 截断提示行
    assert len([ln for ln in r.output.splitlines() if ln.startswith("big.txt")]) == 5


def test_search_missing_pattern(tmp_path):
    r = CodeSearchTool().run({"path": str(tmp_path)}, _ctx())
    assert r.is_error


def test_search_invalid_regex(tmp_path):
    r = CodeSearchTool().run({"pattern": "([", "path": str(tmp_path)}, _ctx())
    assert r.is_error
    assert "正则" in r.output


def test_search_missing_path():
    r = CodeSearchTool().run({"pattern": "x", "path": "/no/such/dir/xyz"}, _ctx())
    assert r.is_error
