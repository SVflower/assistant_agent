"""M7a Skills 系统测试：解析 / 发现 / 加载 / 注入 / 工具 / 边界。"""

from __future__ import annotations

from pathlib import Path

from assistant_agent.agent.prompts import build_system_prompt
from assistant_agent.bootstrap.tools import bounded_skill_metadata
from assistant_agent.config.schema import SkillsConfig
from assistant_agent.integrations.skills.store import SkillStore, _split_frontmatter
from assistant_agent.integrations.skills.tool import LoadSkillTool
from assistant_agent.tools.registry import ToolRegistry
from tests.support import ToolContextFixture


def _write_skill(base: Path, name: str, frontmatter: str, body: str = "正文内容") -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


# ---- frontmatter 解析 ----


def test_split_frontmatter_normal():
    data, body = _split_frontmatter("---\nname: x\ndescription: y\n---\nhello\n")
    assert data == {"name": "x", "description": "y"}
    assert body.strip() == "hello"


def test_split_frontmatter_none_when_no_marker():
    data, body = _split_frontmatter("no frontmatter here")
    assert data == {}
    assert body == "no frontmatter here"


def test_split_frontmatter_invalid_yaml_falls_back():
    data, body = _split_frontmatter("---\n: : bad\n---\nx")
    assert data == {}  # 非法 YAML 不抛，回退空


# ---- 发现 ----


def test_discover_basic(tmp_path):
    _write_skill(tmp_path, "run-tests", "name: run-tests\ndescription: 跑测试")
    store = SkillStore.discover([tmp_path])
    metas = store.list()
    assert len(metas) == 1
    assert metas[0].name == "run-tests"
    assert metas[0].description == "跑测试"


def test_discover_skips_bad_files(tmp_path):
    _write_skill(tmp_path, "good", "name: good\ndescription: ok")
    _write_skill(tmp_path, "no-name", "description: 缺名字")
    _write_skill(tmp_path, "no-desc", "name: nodesc")
    _write_skill(tmp_path, "empty", "")
    store = SkillStore.discover([tmp_path])
    assert [m.name for m in store.list()] == ["good"]


def test_discover_missing_dir_ok(tmp_path):
    store = SkillStore.discover([tmp_path / "nope", tmp_path / "also-nope"])
    assert store.list() == []


def test_discover_dedup_first_wins(tmp_path):
    proj = tmp_path / "proj"
    home = tmp_path / "home"
    _write_skill(proj, "dup", "name: dup\ndescription: 项目级", body="项目正文")
    _write_skill(home, "dup", "name: dup\ndescription: 个人级", body="个人正文")
    store = SkillStore.discover([proj, home])  # proj 在前 → 覆盖
    metas = store.list()
    assert len(metas) == 1
    assert metas[0].description == "项目级"


def test_discover_marks_source_and_trust(tmp_path):
    project = tmp_path / "project"
    personal = tmp_path / "personal"
    _write_skill(project, "project-skill", "name: project-skill\ndescription: 项目")
    _write_skill(personal, "personal-skill", "name: personal-skill\ndescription: 个人")
    store = SkillStore.discover([project, personal], sources=["project", "personal"])
    metas = {meta.name: meta for meta in store.list()}
    assert metas["project-skill"].source == "project"
    assert not metas["project-skill"].trusted
    assert metas["personal-skill"].trusted


def test_discover_rejects_prompt_shaping_name_and_strips_controls(tmp_path):
    _write_skill(tmp_path, "bad", 'name: "bad name"\ndescription: nope')
    _write_skill(tmp_path, "good", 'name: good\ndescription: "hello\\tworld"')
    metas = SkillStore.discover([tmp_path]).list()
    assert [meta.name for meta in metas] == ["good"]
    assert metas[0].description == "hello world"


# ---- 加载正文（Level 2）----


def test_get_body_returns_body_without_frontmatter(tmp_path):
    _write_skill(tmp_path, "s", "name: s\ndescription: d", body="# 步骤\n1. 做事")
    store = SkillStore.discover([tmp_path])
    body = store.get_body("s")
    assert body is not None
    assert "步骤" in body
    assert "name: s" not in body  # frontmatter 已剥离


def test_get_body_unknown_returns_none(tmp_path):
    store = SkillStore.discover([tmp_path])
    assert store.get_body("ghost") is None


# ---- LoadSkillTool ----


def _ctx() -> ToolContextFixture:
    return ToolContextFixture()


def test_load_skill_tool_returns_body(tmp_path):
    _write_skill(tmp_path, "s", "name: s\ndescription: d", body="干活指南")
    tool = LoadSkillTool(SkillStore.discover([tmp_path]))
    result = tool.run({"name": "s"}, _ctx())
    assert not result.is_error
    assert "干活指南" in result.output


def test_load_skill_tool_unknown_errors(tmp_path):
    tool = LoadSkillTool(SkillStore.discover([tmp_path]))
    result = tool.run({"name": "ghost"}, _ctx())
    assert result.is_error
    assert "未知技能" in result.output


def test_load_skill_tool_missing_name(tmp_path):
    tool = LoadSkillTool(SkillStore.discover([tmp_path]))
    result = tool.run({}, _ctx())
    assert result.is_error


def test_load_skill_tool_rejects_path_traversal(tmp_path):
    # 名字里带路径也只当普通名字查，查不到 → error，不读任意文件
    _write_skill(tmp_path, "s", "name: s\ndescription: d")
    tool = LoadSkillTool(SkillStore.discover([tmp_path]))
    result = tool.run({"name": "../../etc/passwd"}, _ctx())
    assert result.is_error


def test_untrusted_skill_cannot_load_noninteractively(tmp_path):
    _write_skill(tmp_path, "s", "name: s\ndescription: d", body="不可信正文")
    tool = LoadSkillTool(SkillStore.discover([tmp_path], sources=["project"]))
    registry = ToolRegistry()
    registry.register(tool)
    result = registry.execute("load_skill", {"name": "s"}, ToolContextFixture(interactive=False))
    assert result.is_error and not result.executed
    assert "不可信正文" not in result.output


# ---- prompt 注入（Level 1）----


def test_prompt_injects_skills():
    prompt = build_system_prompt(True, skills=[("run-tests", "跑测试与 lint")])
    assert "# 可用技能" in prompt
    assert "run-tests" in prompt
    assert "跑测试与 lint" in prompt


def test_prompt_no_skills_section_when_none():
    base = build_system_prompt(True)
    with_none = build_system_prompt(True, skills=None)
    with_empty = build_system_prompt(True, skills=[])
    assert "# 可用技能" not in base
    assert with_none == base  # 回归：None 与原行为一致
    assert with_empty == base


def test_skill_metadata_catalog_is_bounded_and_stable(tmp_path):
    for index in range(20):
        _write_skill(
            tmp_path,
            f"skill-{index:02d}",
            f"name: skill-{index:02d}\ndescription: {'x' * 100}",
        )
    store = SkillStore.discover([tmp_path])
    selected, omitted = bounded_skill_metadata(
        store.list(), SkillsConfig(catalog_max_chars=256), max_context_tokens=1000
    )
    assert selected
    assert omitted
    assert [name for name, _ in selected] == sorted(name for name, _ in selected)
    assert sum(len(name) + len(description) + 4 for name, description in selected) <= 256
