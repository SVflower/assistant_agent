"""架构适应度函数（fitness functions）。

把架构规则写成自动化测试，每次 pytest 就检查架构没被迭代破坏——
架构腐化从"人偶尔发现"变成"CI 立刻报红"。

规则来源：项目分层与 CLAUDE.md 铁律（内核封闭、加能力在 tools/）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "assistant_agent"

# 分层：数字越小越"底层"。每个模块只能依赖同层或更低层（经典分层架构，防环）。
#   config(0) → llm(1) → tools(2) → agent(3) → ui(4) → main(5)
#   session(0)：纯存储，不依赖任何内部模块，与 config 同为底层基础设施。
_LAYER_RANK = {
    "config": 0,
    "session": 0,
    "llm": 1,
    "tools": 2,
    "agent": 3,
    "ui": 4,
    "cli": 5,  # slash 命令：编排 agent/ui/session，供 main 使用
    "main": 6,  # 顶层：main.py / __main__.py
}

# 单文件行数预算：超过就该拆分。当前最大 client.py(≈283)，留少量余量。
# 触发失败时的正确反应是"拆分模块"，而不是"调大这个数"。
_MAX_FILE_LINES = 300


def _iter_src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _module_layer(path: Path) -> str:
    """文件所属层：src/assistant_agent/<layer>/... 取 <layer>；
    直接位于 assistant_agent 下的（main.py 等）归为 'main' 顶层。
    """
    rel = path.relative_to(SRC)
    if len(rel.parts) == 1:
        return "main"
    return rel.parts[0]


def _imported_layers(path: Path) -> set[str]:
    """解析该文件 import 的本项目子包所属层集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    layers: set[str] = set()
    for node in ast.walk(tree):
        module: str | None = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("assistant_agent."):
                    parts = alias.name.split(".")
                    if len(parts) >= 2:
                        layers.add(parts[1])
        if module and module.startswith("assistant_agent."):
            parts = module.split(".")
            if len(parts) >= 2:
                layers.add(parts[1])
    return layers


def test_no_upward_dependencies():
    """任何模块都不得依赖更高层（内核不依赖外壳；防依赖倒置与环）。"""
    violations: list[str] = []
    for path in _iter_src_files():
        importer_layer = _module_layer(path)
        importer_rank = _LAYER_RANK.get(importer_layer)
        if importer_rank is None:
            continue
        for dep in _imported_layers(path):
            dep_rank = _LAYER_RANK.get(dep)
            if dep_rank is None:
                continue
            if dep_rank > importer_rank:
                violations.append(
                    f"{path.relative_to(SRC)}（{importer_layer}）不应依赖更高层 {dep}"
                )
    assert not violations, "架构分层被破坏：\n" + "\n".join(violations)


def test_kernel_is_ui_agnostic():
    """内核 agent/loop.py 绝不依赖 UI——它 yield 事件，不做打印（铁律4）。"""
    layers = _imported_layers(SRC / "agent" / "loop.py")
    assert "ui" not in layers, "agent/loop.py 不应 import ui，内核必须与 UI 解耦"


def test_tools_do_not_depend_on_agent_or_ui():
    """工具是叶子能力，不得反向依赖 agent 循环或 ui（加能力=纯 tools/ 扩展）。"""
    for path in (SRC / "tools").rglob("*.py"):
        layers = _imported_layers(path)
        assert "agent" not in layers, f"{path.name} 不应依赖 agent"
        assert "ui" not in layers, f"{path.name} 不应依赖 ui"


def test_file_size_budget():
    """单文件不超过行数预算；超了应拆分模块，而非调大阈值。"""
    oversized: list[str] = []
    for path in _iter_src_files():
        n = len(path.read_text(encoding="utf-8").splitlines())
        if n > _MAX_FILE_LINES:
            oversized.append(f"{path.relative_to(SRC)}: {n} 行 > {_MAX_FILE_LINES}")
    assert not oversized, "以下文件过大，请拆分：\n" + "\n".join(oversized)
