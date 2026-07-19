"""Provider 和 Tool 的端口边界。"""

from assistant_agent.providers.litellm import LLMClient
from assistant_agent.providers.ports import ModelProviderPort
from assistant_agent.tools.context import ToolContext
from assistant_agent.tools.models import ToolBudget, ToolResult


def test_provider_adapter_implements_public_port_shape():
    assert callable(LLMClient.complete_stream)
    assert hasattr(ModelProviderPort, "complete_stream")


def test_tool_context_requires_explicit_runtime_ports():
    required = {"workspace", "run_control", "logger", "artifact_store"}
    assert required <= ToolContext.__dataclass_fields__.keys()
    assert ToolBudget(max_calls=1).max_calls == 1
    assert ToolResult.ok("done").output == "done"
