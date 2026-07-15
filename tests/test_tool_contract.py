"""M10a 工具结果契约与 Registry 参数校验。"""

from __future__ import annotations

import pytest

from assistant_agent.tools.base import ArtifactRef, Tool, ToolBudget, ToolContext, ToolResult
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.validation import ToolSchemaError


class RecordingTool(Tool):
    name = "record"
    description = "test"

    def __init__(self) -> None:
        self.permission_calls = 0
        self.run_calls = 0

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["safe"]},
                "count": {"type": "integer", "minimum": 1},
            },
            "required": ["mode"],
        }

    def permission_requests(self, args, ctx):
        self.permission_calls += 1
        return []

    def run(self, args, ctx):
        self.run_calls += 1
        return ToolResult.ok(
            "abcdefghij",
            metadata={"source": "test"},
            artifacts=[ArtifactRef("a", ".assistant_agent/artifacts/a.txt", size_chars=10)],
        )


def test_tool_result_old_constructor_remains_compatible():
    assert ToolResult("ok").code == "ok"
    assert ToolResult("bad", is_error=True).code == "tool_error"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"mode": "unsafe"},
        {"mode": 123},
        {"mode": "safe", "count": 0},
    ],
)
def test_validation_happens_before_permission_and_side_effect(args):
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    result = registry.execute(tool.name, args, ToolContext())
    assert result.code == "invalid_arguments"
    assert result.retryable is True
    assert result.executed is False
    assert tool.permission_calls == 0
    assert tool.run_calls == 0


def test_validation_allows_unknown_fields_for_small_model_tolerance():
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    result = registry.execute("record", {"mode": "safe", "extra": "ignored"}, ToolContext())
    assert result.code == "ok"
    assert tool.run_calls == 1


def test_registry_rejects_invalid_tool_schema():
    class BadSchema(RecordingTool):
        name = "bad_schema"

        @property
        def parameters(self):
            return {"type": "not-a-json-schema-type"}

    with pytest.raises(ToolSchemaError, match="schema 无效"):
        ToolRegistry().register(BadSchema())


def test_output_limiting_preserves_structured_fields():
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register(tool)
    result = registry.execute("record", {"mode": "safe"}, ToolContext(max_output_chars=5))
    assert len(result.output) == 5
    assert result.metadata == {"source": "test"}
    assert result.artifacts[0].id == "a"
    assert result.code == "ok"


def test_budget_and_unknown_tool_have_stable_codes():
    registry = ToolRegistry()
    tool = RecordingTool()
    registry.register(tool)
    budget = ToolBudget(max_calls=1, used_calls=1)
    exhausted = registry.execute("record", {"mode": "safe"}, ToolContext(budget=budget))
    unknown = registry.execute("missing", {}, ToolContext())
    assert exhausted.code == "budget_exhausted"
    assert exhausted.executed is False
    assert unknown.code == "unknown_tool"
