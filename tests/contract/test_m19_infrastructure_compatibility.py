"""M19f 基础设施旧路径保持 identity-compatible。"""

import assistant_agent.mcp.manager as legacy_mcp_manager
import assistant_agent.runtime.control as legacy_runtime_control
import assistant_agent.skills.store as legacy_skill_store
import assistant_agent.web.client as legacy_web_client
from assistant_agent.session.store import SessionStore as LegacySessionStore

import assistant_agent.execution.control as execution_control
import assistant_agent.integrations.mcp.manager as integration_mcp_manager
import assistant_agent.integrations.skills.store as integration_skill_store
import assistant_agent.integrations.web_access.client as integration_web_client
from assistant_agent.execution import RunControl
from assistant_agent.integrations.mcp import MCPManager
from assistant_agent.integrations.skills import SkillStore
from assistant_agent.integrations.web_access import WebClient
from assistant_agent.mcp import MCPManager as LegacyMCPManager
from assistant_agent.obs import EventLogger as LegacyEventLogger
from assistant_agent.observability import EventLogger
from assistant_agent.persistence.artifacts import ArtifactStore
from assistant_agent.persistence.store import SessionStore
from assistant_agent.runtime import RunControl as LegacyRunControl
from assistant_agent.skills import SkillStore as LegacySkillStore
from assistant_agent.tools.artifacts import ArtifactStore as LegacyArtifactStore
from assistant_agent.web import WebClient as LegacyWebClient


def test_legacy_infrastructure_symbols_are_identity_aliases() -> None:
    assert LegacyRunControl is RunControl
    assert LegacySessionStore is SessionStore
    assert LegacyEventLogger is EventLogger
    assert LegacyMCPManager is MCPManager
    assert LegacySkillStore is SkillStore
    assert LegacyWebClient is WebClient
    assert LegacyArtifactStore is ArtifactStore


def test_legacy_infrastructure_submodules_are_identity_aliases() -> None:
    assert legacy_runtime_control is execution_control
    assert legacy_mcp_manager is integration_mcp_manager
    assert legacy_skill_store is integration_skill_store
    assert legacy_web_client is integration_web_client
