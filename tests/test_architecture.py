"""架构适应度函数（fitness functions）。

把架构规则写成自动化测试，每次 pytest 就检查架构没被迭代破坏——
架构腐化从"人偶尔发现"变成"CI 立刻报红"。

规则来源：项目分层与 CLAUDE.md 铁律（内核封闭、加能力在 tools/）。
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "assistant_agent"

# 分层：数字越小越"底层"。每个模块只能依赖同层或更低层（经典分层架构，防环）。
#   interaction/config → llm/runtime → tools → agent → service → ui → cli → main
#   session(0)：纯存储，不依赖任何内部模块，与 config 同为底层基础设施。
#   obs(0)：可观测性（结构化日志/审计），底层基础设施，被 tools/agent/main 使用，不依赖上层。
_LAYER_RANK = {
    "config": 0,
    "session": 0,
    "obs": 0,
    "interaction": 0,  # 纯同步协议/DTO，不依赖 Agent 实现或 UI
    "llm": 1,
    "runtime": 1,  # 低层运行控制、进程监管与 Workspace，不依赖 tools/agent/ui
    "web": 1,  # HTTP 搜索/抓取基础设施，只依赖 config 与外部 httpx
    "tools": 2,
    "skills": 2,  # 叶子能力层：SKILL.md 发现 + load_skill 工具，只依赖 tools/base
    "mcp": 2,  # 叶子能力层：MCP client，MCPTool 继承 tools/base，只依赖它 + 外部 mcp 包
    "agent": 3,
    "service": 4,  # 公共装配与 Session/Run 编排，不依赖 CLI/UI
    "ui": 5,
    "cli": 6,  # slash 命令：编排 service/ui，供 main 使用
    "main": 7,  # 顶层：main.py / __main__.py
}

# 单文件行数预算：分级软/硬。
#   软线 300：超过打印警告——这是交给人评审的"重构信号"（检查职责是否内聚），不 fail。
#     一味为凑行数拆内聚的状态机/声明性代码反而更糟，故不做硬阻断。
#   硬线 500：超过直接 fail——防膨胀刹车。触发时的正确反应是"拆分模块"，而非调大此数。
_SOFT_FILE_LINES = 300
_HARD_FILE_LINES = 500


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


def test_service_is_ui_and_cli_agnostic():
    """公共 Python 服务边界不能依赖终端外壳。"""
    for path in (SRC / "service").rglob("*.py"):
        layers = _imported_layers(path)
        assert "ui" not in layers, f"{path.name} 不应依赖 ui"
        assert "cli" not in layers, f"{path.name} 不应依赖 cli"


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


def test_file_size_budget():
    """单文件行数分级：超软线仅警告（交人评审），超硬线才失败（防膨胀）。

    软线是"重构信号"不阻断——避免为凑行数硬拆内聚代码；硬线是刹车，
    触发时应拆分模块而非调大阈值。
    """
    hard: list[str] = []
    soft: list[str] = []
    for path in _iter_src_files():
        n = len(path.read_text(encoding="utf-8").splitlines())
        if n > _HARD_FILE_LINES:
            hard.append(f"{path.relative_to(SRC)}: {n} 行 > 硬线 {_HARD_FILE_LINES}")
        elif n > _SOFT_FILE_LINES:
            soft.append(f"{path.relative_to(SRC)}: {n} 行 > 软线 {_SOFT_FILE_LINES}")
    if soft:
        warnings.warn(
            "以下文件超过软线，建议评审是否拆分（不阻断）：\n" + "\n".join(soft),
            stacklevel=2,
        )
    assert not hard, "以下文件超过硬线，请拆分模块（勿调大阈值）：\n" + "\n".join(hard)
