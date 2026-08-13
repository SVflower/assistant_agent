"""M17 RuntimePolicy 与能力快照测试。"""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from assistant_agent.integrations.web_access.client import SearchResult
from assistant_agent.interaction import ApprovalDecision, BlockingInteractionPort
from assistant_agent.service import (
    AgentService,
    RuntimeDependencyError,
    RuntimePolicy,
    RuntimePolicyError,
    create_runtime,
)


class _SearchBackend:
    name = "test"
    network_target = "https://search.example"

    def search(self, query: str, max_results: int, freshness: str | None):
        del max_results, freshness
        return [SearchResult(query, "https://example.com/result", "summary", "test")]


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
    _skill(tmp_path, "skills", "project")
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
        runtime_policy=RuntimePolicy(
            allowed_mcp_transports=frozenset(), allow_personal_skills=False
        ),
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


def test_web_profile_only_exposes_server_safe_tools_and_search_needs_no_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(home))
    _skill(home, "skills", "admin-installed")
    config = _config(
        tmp_path / "config.yaml",
        "agent:\n  max_context_tokens: 16000\n"
        "sandbox:\n  mode: workspace\n"
        "permissions:\n  mode: strict\n",
    )
    port = BlockingInteractionPort(timeout=1)
    policy = RuntimePolicy.web()
    runtime = create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interaction=port,
        interactive=True,
        runtime_policy=policy,
    )
    try:
        assert runtime.capabilities is not None
        assert runtime.capabilities.profile == "web"
        assert [item.name for item in runtime.capabilities.skills] == ["admin-installed"]
        assert runtime.capabilities.skills[0].source == "personal"
        names = set(runtime.capabilities.tools)
        assert "web_search" in names
        assert "present_chart" in names
        assert runtime.capabilities.chart_spec_versions == (2,)
        assert "present_chart" in {item["function"]["name"] for item in runtime.loop.tool_schemas}
        assert (
            not {
                "read_file",
                "write_file",
                "edit_file",
                "multi_edit",
                "list_dir",
                "code_search",
                "git",
                "run_shell",
                "manage_process",
                "manage_skill",
                "configure_mcp_server",
                "fetch_url",
            }
            & names
        )
        assert runtime.web is not None
        loaded = runtime.loop._registry.execute(  # noqa: SLF001
            "load_skill", {"name": "admin-installed"}, runtime.tool_context
        )
        assert loaded.is_error is False
        assert "body" in loaded.output
        runtime.web.backend = _SearchBackend()  # type: ignore[attr-defined]
        result = runtime.loop._registry.execute(  # noqa: SLF001 - verifies composed registry
            "web_search", {"query": "agent"}, runtime.tool_context
        )
        assert result.is_error is False
        assert port.next_request(timeout=0.01) is None
        blocked = runtime.loop._registry.execute(  # noqa: SLF001
            "run_shell", {"command": "whoami"}, runtime.tool_context
        )
        assert blocked.code == "unknown_tool" and blocked.executed is False
        assert all("profile" not in str(schema) for schema in runtime.loop.tool_schemas)
        assert "write_file(path" not in runtime.loop.system_prompt
        with pytest.raises(FrozenInstanceError):
            policy.profile = "cli"  # type: ignore[misc]
    finally:
        runtime.close()


def test_cli_profile_keeps_network_approval_and_full_local_tools(tmp_path: Path) -> None:
    config = _config(tmp_path / "config.yaml", "agent:\n  max_context_tokens: 16000\n")
    port = BlockingInteractionPort(timeout=1)
    runtime = create_runtime(
        config_path=config,
        workspace_root=tmp_path,
        interaction=port,
        interactive=True,
        runtime_policy=RuntimePolicy.cli(),
    )
    result = []
    try:
        assert runtime.capabilities is not None
        assert runtime.capabilities.profile == "cli"
        assert {"read_file", "write_file", "run_shell", "manage_process"} <= set(
            runtime.capabilities.tools
        )
        assert "present_chart" not in runtime.capabilities.tools
        assert runtime.capabilities.chart_spec_versions == ()
        assert "present_chart" not in {
            item["function"]["name"] for item in runtime.loop.tool_schemas
        }
        worker = threading.Thread(
            target=lambda: result.append(
                runtime.loop._registry.execute(  # noqa: SLF001 - verifies composed registry
                    "web_search", {"query": "agent"}, runtime.tool_context
                )
            ),
            daemon=True,
        )
        worker.start()
        request = port.next_request(timeout=0.5)
        assert request is not None and request.kind == "approval"
        assert request.tool == "web_search"
        assert port.respond(ApprovalDecision(request.request_id, "deny")) is True
        worker.join(timeout=1)
        assert result and result[0].code == "permission_denied"
    finally:
        runtime.close()
