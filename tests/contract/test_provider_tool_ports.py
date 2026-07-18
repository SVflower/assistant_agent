"""Provider/Tool 拆分后的兼容导入与端口边界。"""

from assistant_agent.llm.client import LLMClient as LegacyLLMClient
from assistant_agent.llm.client import StreamEvent as LegacyStreamEvent
from assistant_agent.providers.litellm import LLMClient
from assistant_agent.providers.ports import ModelProviderPort, StreamEvent
from assistant_agent.tools.base import ToolBudget as LegacyToolBudget
from assistant_agent.tools.base import ToolContext as LegacyToolContext
from assistant_agent.tools.base import ToolResult as LegacyToolResult
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.models import ToolBudget, ToolResult


def test_provider_legacy_exports_are_identity_aliases():
    assert LegacyLLMClient is LLMClient
    assert LegacyStreamEvent is StreamEvent
    assert hasattr(ModelProviderPort, "complete_stream")


def test_tool_models_keep_identity_and_context_compatibility():
    assert LegacyToolBudget is ToolBudget
    assert LegacyToolResult is ToolResult
    assert issubclass(LegacyToolContext, ToolContext)
    context = LegacyToolContext()
    try:
        assert context.workspace.root == context.workspace_root
        assert context.artifact_store is not None
    finally:
        context.workspace.close()
