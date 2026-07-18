"""架构适应度函数（fitness functions）。

把架构规则写成自动化测试，每次 pytest 就检查架构没被迭代破坏——
架构腐化从"人偶尔发现"变成"CI 立刻报红"。

通用包依赖由 import-linter 负责；这里保留项目专属 AST 规则与非阻断预警。
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "assistant_agent"

_REVIEW_FILE_LINES = 600


def _iter_src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


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


def test_contracts_have_no_project_dependencies():
    """AST 兜底：新增项目包也不能被 contracts 反向导入。"""
    dependencies: dict[str, set[str]] = {}
    for path in (SRC / "contracts").rglob("*.py"):
        imported = _imported_layers(path) - {"contracts"}
        if imported:
            dependencies[str(path.relative_to(SRC))] = imported
    assert not dependencies, f"contracts 必须是项目最低层：{dependencies}"


def test_kernel_is_ui_agnostic():
    """内核 agent/loop.py 绝不依赖 UI——它 yield 事件，不做打印（铁律4）。"""
    layers = _imported_layers(SRC / "agent" / "loop.py")
    assert "ui" not in layers, "agent/loop.py 不应 import ui，内核必须与 UI 解耦"


def test_service_is_ui_and_cli_agnostic():
    """公共 Python 服务边界不能依赖终端外壳。"""
    for path in (SRC / "service").rglob("*.py"):
        layers = _imported_layers(path)
        assert "ui" not in layers, f"{path.name} 不应依赖 ui"
        assert "cli" not in layers, f"{path.name} 不应依赖 cli"


def test_service_root_is_definition_free_facade():
    """Service root 只能转发稳定入口，不得重新长出编排或状态。"""
    path = SRC / "service" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definitions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert definitions == []


def test_tools_do_not_depend_on_agent_or_ui():
    """工具是叶子能力，不得反向依赖 agent 循环或 ui（加能力=纯 tools/ 扩展）。"""
    for path in (SRC / "tools").rglob("*.py"):
        layers = _imported_layers(path)
        assert "agent" not in layers, f"{path.name} 不应依赖 agent"
        assert "ui" not in layers, f"{path.name} 不应依赖 ui"


def test_business_mcp_servers_are_external():
    """业务 MCP 必须独立部署，Agent 仓库只实现通用 MCP client。"""
    project_root = SRC.parents[1]
    assert not (project_root / "mcp_servers").exists(), (
        "业务 MCP 不得内嵌在 Agent 仓库；请迁移到独立 MCP 工作区"
    )


def test_large_modules_are_reported_for_review():
    """超过 600 行只预警；是否拆分由内聚性评审决定。"""
    large: list[str] = []
    for path in _iter_src_files():
        n = len(path.read_text(encoding="utf-8").splitlines())
        if n > _REVIEW_FILE_LINES:
            large.append(f"{path.relative_to(SRC)}: {n} 行")
    if large:
        warnings.warn(
            "以下文件超过 600 行，请在 docs/ARCHITECTURE.md 记录内聚性评审（不阻断）：\n"
            + "\n".join(large),
            stacklevel=2,
        )
