"""M17 RuntimePolicy 与能力快照测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant_agent.service import (
    AgentService,
    RuntimeDependencyError,
    RuntimePolicy,
    RuntimePolicyError,
    create_runtime,
)


def _config(path: Path, extra: str = "") -> Path:
    path.write_text(
        "active: p\nproviders:\n  p:\n    model: openai/fake\n" + extra,
        encoding="utf-8",
    )
    return path


def _skill(root: Path, relative: str, name: str) -> None:
    target = root / relative / name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\nbody",
        encoding="utf-8",
    )


def test_policy_rejects_weaker_sandbox_before_runtime_resources(tmp_path: Path) -> None:
    config = _config(tmp_path / "config.yaml", "sandbox:\n  mode: off\n")
    with pytest.raises(RuntimePolicyError, match="低于调用方要求"):
        create_runtime(
            config_path=config,
            workspace_root=tmp_path,
            interactive=False,
            runtime_policy=RuntimePolicy(minimum_sandbox="workspace"),
        )


def test_service_policy_hides_extension_tools_and_personal_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(home))
    _skill(home, "skills", "personal")
    _skill(tmp_path, ".agents/skills", "project")
    config = _config(
        tmp_path / "config.yaml",
        "agent:\n  max_context_tokens: 16000\n"
        "sandbox:\n  mode: workspace\n"
        "skills:\n  trusted_project_skills: [project]\n",
    )
    runtime = create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interactive=False,
        runtime_policy=RuntimePolicy.service(),
    )
    try:
        assert [item.name for item in runtime.visible_skills] == ["project"]
        assert runtime.capabilities is not None
        assert runtime.capabilities.extension_management is False
        assert "manage_skill" not in runtime.capabilities.tools
        assert "configure_mcp_server" not in runtime.capabilities.tools
        assert [item.name for item in runtime.capabilities.skills] == ["project"]
        assert len(runtime.capabilities.skills[0].fingerprint) == 12
    finally:
        runtime.close()


def test_disallowed_mcp_transport_is_visible_but_not_started(tmp_path: Path) -> None:
    config = _config(
        tmp_path / "config.yaml",
        "mcp:\n  servers:\n    local:\n      type: stdio\n      command: never-run\n",
    )
    runtime = create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interactive=False,
        runtime_policy=RuntimePolicy(allowed_mcp_transports=frozenset()),
    )
    try:
        assert runtime.capabilities is not None
        status = runtime.capabilities.mcp_servers[0]
        assert status.name == "local"
        assert status.status == "blocked_by_policy"
        assert status.error_category == "policy"
        assert "never-run" not in repr(status)
    finally:
        runtime.close()


def test_agent_service_propagates_policy_and_probe_closes_runtime(tmp_path: Path) -> None:
    config = _config(tmp_path / "config.yaml", "sandbox:\n  mode: workspace\n")
    service = AgentService(
        config_path=config,
        workspace_root=tmp_path,
        runtime_policy=RuntimePolicy.service(),
    )
    snapshot = service.probe_capabilities()
    assert snapshot.sandbox == "workspace"
    assert snapshot.extension_management is False


def test_required_mcp_blocked_by_policy_is_typed_dependency_error(tmp_path: Path) -> None:
    config = _config(
        tmp_path / "config.yaml",
        "mcp:\n  servers:\n    critical:\n      command: never-run\n      startup: required\n",
    )
    with pytest.raises(RuntimeDependencyError) as raised:
        create_runtime(
            config_path=config,
            workspace_root=tmp_path,
            interactive=False,
            runtime_policy=RuntimePolicy(allowed_mcp_transports=frozenset()),
        )
    assert raised.value.dependency == "critical"
    assert raised.value.category == "policy"
