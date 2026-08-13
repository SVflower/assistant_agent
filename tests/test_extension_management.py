"""M11c Skill/MCP 扩展管理的事务与边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant_agent.config.loader import load_config
from assistant_agent.config.paths import state_paths
from assistant_agent.config.schema import MCPServerConfig
from assistant_agent.config.writer import ConfigWriteError, MCPConfigStore, SkillsConfigStore
from assistant_agent.integrations.mcp.configure import MCPConfigureError, MCPProbeResult, MCPService
from assistant_agent.integrations.skills.manager import SkillInstallError, SkillManager
from assistant_agent.integrations.skills.store import SkillStore
from assistant_agent.tools.extensions import ConfigureMCPServerTool, ManageSkillTool
from assistant_agent.tools.permissions import Capability
from tests.support import ToolContextFixture

_CONFIG = """
# keep this project comment
active: local
providers:
  local:
    model: openai/test
mcp:
  enabled: true
  servers: {}
"""


def _skill(root: Path, name: str = "sample", body: str = "执行测试") -> Path:
    source = root / "source" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试技能\n---\n{body}\n", encoding="utf-8"
    )
    return source


def _project_config(tmp_path: Path) -> Path:
    path = tmp_path / "workspace" / "config.yaml"
    path.parent.mkdir()
    path.write_text(_CONFIG, encoding="utf-8")
    return path


def test_skill_user_install_discover_idempotent_and_remove(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(home))
    manager = SkillManager(tmp_path / "workspace")
    source = _skill(tmp_path)

    first = manager.install(source)
    second = manager.install(source)

    assert first.changed is True and second.changed is False
    store = SkillStore.discover([home / "skills"], sources=["personal"])
    assert store.get_body("sample").strip() == "执行测试"
    assert manager.uninstall("sample") is True
    assert not first.path.exists()


def test_skill_project_scope_and_same_name_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    manager = SkillManager(workspace)
    user_source = _skill(tmp_path / "user", body="用户版本")
    project_source = _skill(tmp_path / "project", body="项目版本")
    manager.install(user_source, "user")
    manager.install(project_source, "project")

    roots = [workspace / ".agents" / "skills", tmp_path / "home" / "skills"]
    assert SkillStore.discover(roots).get_body("sample").strip() == "项目版本"
    manager.uninstall("sample", "project")
    assert SkillStore.discover(roots).get_body("sample").strip() == "用户版本"


def test_project_skill_validation_rejects_missing_invalid_mismatch_and_escape(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    manager = SkillManager(workspace)

    with pytest.raises(SkillInstallError, match="不存在或 SKILL.md 无效"):
        manager.project_skill("missing")

    invalid = workspace / ".agents" / "skills" / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_text("not frontmatter", encoding="utf-8")
    with pytest.raises(SkillInstallError, match="不存在或 SKILL.md 无效"):
        manager.project_skill("invalid")

    mismatch = workspace / ".agents" / "skills" / "folder-name"
    mismatch.mkdir(parents=True)
    (mismatch / "SKILL.md").write_text(
        "---\nname: declared-name\ndescription: mismatch\n---\nbody\n", encoding="utf-8"
    )
    with pytest.raises(SkillInstallError, match="必须一致"):
        manager.project_skill("folder-name")

    with pytest.raises(SkillInstallError, match="路径逃逸"):
        manager.project_skill("../outside")


def test_skills_config_store_is_sorted_idempotent_and_preserves_comments(tmp_path):
    config_path = _project_config(tmp_path)
    store = SkillsConfigStore(config_path)

    assert store.set_trusted("zeta", True) is True
    assert store.set_trusted("alpha", True) is True
    assert store.set_trusted("zeta", True) is False
    assert store.trusted() == ("alpha", "zeta")
    assert "# keep this project comment" in config_path.read_text(encoding="utf-8")
    assert load_config(config_path).skills.trusted_project_skills == ["alpha", "zeta"]

    assert store.set_trusted("zeta", False) is True
    assert store.set_trusted("zeta", False) is False
    assert store.trusted() == ("alpha",)


def test_skills_config_store_write_failure_keeps_file_and_loaded_config(tmp_path, monkeypatch):
    config_path = _project_config(tmp_path)
    original = config_path.read_bytes()
    loaded = load_config(config_path)
    store = SkillsConfigStore(config_path)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("assistant_agent.config.writer._atomic_yaml_dump", fail_write)
    with pytest.raises(OSError, match="disk unavailable"):
        store.set_trusted("demo", True)

    assert config_path.read_bytes() == original
    assert loaded.skills.trusted_project_skills == []


def test_skill_discovery_reports_invalid_and_shadowed_entries(tmp_path):
    project = tmp_path / "project"
    personal = tmp_path / "personal"
    for root in (project, personal):
        (root / "demo").mkdir(parents=True)
        (root / "demo" / "SKILL.md").write_text(
            "---\nname: demo\ndescription: valid\n---\nbody\n", encoding="utf-8"
        )
    (project / "invalid").mkdir()
    invalid = project / "invalid" / "SKILL.md"
    invalid.write_text("missing frontmatter", encoding="utf-8")

    store = SkillStore.discover([project, personal], sources=["project", "personal"])

    meta = store.get_meta("demo")
    assert meta is not None and meta.source == "project"
    assert store.report.conflicts == ("demo",)
    assert store.report.invalid == (str(invalid),)


def test_skill_rejects_unmanaged_conflict_and_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    manager = SkillManager(tmp_path / "workspace")
    unmanaged = tmp_path / "home" / "skills" / "sample"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text("manual", encoding="utf-8")
    with pytest.raises(SkillInstallError, match="目标已存在"):
        manager.install(_skill(tmp_path))
    with pytest.raises(SkillInstallError, match="非受管"):
        manager.uninstall("sample")

    source = _skill(tmp_path / "linked", "linked")
    try:
        (source / "escape").symlink_to(tmp_path / "outside")
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(SkillInstallError, match="符号链接"):
        manager.install(source)


def test_manage_skill_tool_reports_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    tool = ManageSkillTool(SkillManager(tmp_path / "workspace"))
    result = tool.run({"action": "install", "source": str(_skill(tmp_path))}, ToolContextFixture())
    assert not result.is_error
    assert "loaded=false" in result.output
    assert result.metadata["reload_command"] == "/reload skills"


def test_mcp_config_store_preserves_comments_and_merges_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    config_path = _project_config(tmp_path)
    store = MCPConfigStore(config_path)
    store.add("shared", MCPServerConfig(command="project"), "project")
    store.add("shared", MCPServerConfig(command="user"), "user")
    store.add("personal", MCPServerConfig(command="user-only"), "user")

    text = config_path.read_text(encoding="utf-8")
    assert "# keep this project comment" in text
    loaded = load_config(config_path)
    assert loaded.mcp.servers["shared"].command == "project"
    assert loaded.mcp.servers["personal"].command == "user-only"
    assert store.list_scoped()["shared"][0] == "project"


def test_mcp_config_store_invalid_candidate_keeps_original(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    config_path = _project_config(tmp_path)
    original = config_path.read_bytes()
    store = MCPConfigStore(config_path)
    with pytest.raises(ConfigWriteError):
        store.add("bad/name", MCPServerConfig(command="x"), "project")
    assert config_path.read_bytes() == original


def test_mcp_service_add_remove_manifest_and_secret_rejection(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(home))
    service = MCPService(_project_config(tmp_path))
    monkeypatch.setattr(
        service,
        "probe",
        lambda name, server: MCPProbeResult(name, (f"mcp__{name}__tool",), ()),
    )

    result = service.add("demo", MCPServerConfig(command="npx", args=["pkg@1"]), "user")
    assert result.tools == ("mcp__demo__tool",)
    assert service.store.get("demo", "user") is not None
    assert (home / "mcp" / "servers" / "demo" / "user.json").is_file()
    assert service.remove("demo", "user") is True
    assert service.store.get("demo", "user") is None
    assert not (home / "mcp" / "servers" / "demo").exists()

    with pytest.raises(MCPConfigureError, match="明文"):
        service.add(
            "secret",
            MCPServerConfig(command="x", env={"TOKEN": "plain-secret"}),
            verify=True,
        )
    with pytest.raises(MCPConfigureError, match="HTTP header"):
        service.add(
            "header-secret",
            MCPServerConfig(command="x", headers={"X-Auth": "plain-secret"}),
            verify=True,
        )

    literal = MCPServerConfig(
        command="x",
        env={"BASE_URL": "https://api.example.com", "READ_ONLY": "true"},
        headers={"Authorization": "Bearer ${API_TOKEN}"},
    )
    result = service.add("literal", literal, verify=True)
    assert result.tools == ("mcp__literal__tool",)
    assert service.store.get("literal", "user").env["BASE_URL"] == "https://api.example.com"


def test_mcp_service_enable_and_trust_are_scope_specific(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    service = MCPService(_project_config(tmp_path))
    service.store.add("demo", MCPServerConfig(command="x"), "project")

    assert service.set_enabled("demo", False, "project").enabled is False
    assert service.set_trusted("demo", True, "project").auto_approve is True
    with pytest.raises(MCPConfigureError, match="不存在"):
        service.set_enabled("demo", True, "user")


def test_mcp_service_purges_only_named_workspace_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    config = _project_config(tmp_path)
    runtime_workspace = tmp_path / "runtime-workspace"
    runtime_workspace.mkdir()
    service = MCPService(config, workspace_root=runtime_workspace)
    root = state_paths(runtime_workspace).mcp_artifacts
    target = root / "demo"
    other = root / "other"
    target.mkdir(parents=True)
    other.mkdir()
    (target / "page.yml").write_text("page", encoding="utf-8")
    (other / "keep.txt").write_text("keep", encoding="utf-8")

    assert service.purge_artifacts("demo") is True
    assert not target.exists() and (other / "keep.txt").is_file()
    assert service.purge_artifacts("demo") is False


def test_configure_mcp_tool_declares_specific_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    tool = ConfigureMCPServerTool(MCPService(_project_config(tmp_path)))
    args = {
        "action": "add",
        "name": "demo",
        "scope": "user",
        "server": {"command": "npx", "args": ["-y", "pkg@1"]},
    }
    requests = tool.permission_requests(args, ToolContextFixture())
    assert {request.capability for request in requests} == {
        Capability.PROCESS_EXECUTE,
        Capability.FILESYSTEM_WRITE,
        Capability.EXTENSION_MANAGE,
    }
    assert any("npx -y pkg@1" == request.display_target for request in requests)


def test_configure_mcp_tool_add_and_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    service = MCPService(_project_config(tmp_path))
    monkeypatch.setattr(
        service,
        "probe",
        lambda name, server: MCPProbeResult(name, ("mcp__demo__tool",), ()),
    )
    tool = ConfigureMCPServerTool(service)
    added = tool.run(
        {
            "action": "add",
            "name": "demo",
            "server": {"command": "npx", "args": ["pkg@1"]},
        },
        ToolContextFixture(),
    )
    removed = tool.run({"action": "remove", "name": "demo"}, ToolContextFixture())
    assert not added.is_error and "connected=false" in added.output
    assert added.metadata["reload_command"] == "/reload mcp"
    assert not removed.is_error and service.store.get("demo", "user") is None


def test_configure_mcp_tool_lists_safe_server_metadata_without_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_AGENT_HOME", str(tmp_path / "home"))
    service = MCPService(_project_config(tmp_path))
    service.store.add(
        "demo",
        MCPServerConfig(
            command="secret-command",
            env={"TOKEN": "secret-value"},
            startup="optional",
        ),
        "user",
    )
    tool = ConfigureMCPServerTool(service)

    result = tool.run({"action": "list"}, ToolContextFixture())

    assert not result.is_error
    assert "demo" in result.output
    assert "secret-command" not in result.output
    assert "secret-value" not in result.output
    assert result.metadata["servers"] == [
        {
            "name": "demo",
            "scope": "user",
            "transport": "stdio",
            "startup": "optional",
            "enabled": True,
            "trusted": False,
        }
    ]
    assert tool.permission_requests({"action": "list"}, ToolContextFixture()) == []
