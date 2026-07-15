"""用户安装目录和 workspace state 路径测试。"""

from __future__ import annotations

from pathlib import Path

from assistant_agent.cli.setup import _discover_skills
from assistant_agent.config.paths import (
    assistant_home,
    managed_mcp_dir,
    project_skills_dir,
    resolve_log_dir,
    resolve_run_dir,
    state_paths,
    user_skills_dir,
    workspace_id,
)
from assistant_agent.config.schema import SkillsConfig
from assistant_agent.session.run_store import RunStore
from assistant_agent.session.store import SessionStore


def _write_skill(base: Path, name: str, description: str) -> None:
    target = base / name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n", encoding="utf-8"
    )


def test_state_paths_are_stable_and_workspace_isolated(tmp_path, monkeypatch):
    home = tmp_path / "agent-home"
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(home))
    first = tmp_path / "one" / "same"
    second = tmp_path / "two" / "same"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    a = state_paths(first)
    b = state_paths(second)
    assert a.home == home.resolve()
    assert a.workspace != b.workspace
    assert a.sessions.parent == a.workspace
    assert a.tool_artifacts == a.workspace / "artifacts" / "tools"
    assert workspace_id(first) == workspace_id(first)


def test_install_roots_are_separate_from_runtime_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    assert user_skills_dir() == assistant_home() / "skills"
    assert managed_mcp_dir() == assistant_home() / "mcp" / "servers"
    assert project_skills_dir(workspace) == workspace / ".agents" / "skills"
    assert user_skills_dir() not in state_paths(workspace).workspace.parents


def test_legacy_default_run_and_log_paths_resolve_to_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    paths = state_paths(tmp_path)
    assert resolve_run_dir(".assistant_agent/runs", tmp_path) == paths.runs
    assert resolve_log_dir(".assistant_agent/logs", tmp_path) == paths.logs
    custom = tmp_path / "custom"
    assert resolve_run_dir(str(custom), tmp_path) == custom


def test_default_stores_use_isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    session_store = SessionStore()
    session = session_store.new_session()
    session_store.save(session, [])
    run_store = RunStore()
    assert Path(session_store._dir).is_relative_to((tmp_path / "home").resolve())
    assert Path(run_store._dir).is_relative_to((tmp_path / "home").resolve())


def test_skill_discovery_prefers_project_then_user_then_legacy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    project = tmp_path / ".agents" / "skills"
    legacy = tmp_path / ".assistant_agent" / "skills"
    _write_skill(project, "dup", "project")
    _write_skill(user_skills_dir(), "dup", "user")
    _write_skill(legacy, "old", "legacy")
    metas = {meta.name: meta for meta in _discover_skills(SkillsConfig()).list()}
    assert metas["dup"].source == "project"
    assert metas["old"].source == "legacy"
    assert not metas["old"].trusted
