"""M13a 声明式工具适配层。"""

from __future__ import annotations

from typing import Literal

import pytest

from assistant_agent.tools import FunctionTool, ToolContext, ToolRegistry, ToolResult, agent_tool
from assistant_agent.tools.base import ToolBudget
from assistant_agent.tools.permissions import Capability, PermissionRequest
from assistant_agent.tools.policy import PermissionPolicy


def _allow(_args, _ctx):
    return []


def test_decorator_builds_schema_from_annotations_defaults_and_docstring():
    @agent_tool
    def summarize(
        values: list[int],
        mode: Literal["short", "full"],
        note: str | None = None,
        limit: int = 3,
    ) -> str:
        """汇总一组数字。"""
        return f"{mode}:{values[:limit]}:{note}"

    assert isinstance(summarize, FunctionTool)
    assert summarize.name == "summarize"
    assert summarize.description == "汇总一组数字。"
    schema = summarize.parameters
    assert set(schema["required"]) == {"values", "mode"}
    assert schema["properties"]["mode"]["enum"] == ["short", "full"]
    assert schema["properties"]["limit"]["default"] == 3
    assert "null" in schema["properties"]["note"]["anyOf"][1]["type"]


def test_decorator_allows_explicit_name_and_description():
    @agent_tool(name="lookup_record", description="按编号查询记录", permissions=_allow)
    def lookup(record_id: str) -> str:
        return record_id

    assert lookup.name == "lookup_record"
    assert lookup.description == "按编号查询记录"


def test_registry_executes_with_defaults_and_ignores_unknown_fields():
    seen = {}

    @agent_tool(permissions=_allow)
    def add(left: int, right: int = 2) -> str:
        seen.update(left=left, right=right)
        return str(left + right)

    registry = ToolRegistry()
    registry.register(add)
    result = registry.execute("add", {"left": 3, "extra": "ignored"}, ToolContext())
    assert result.output == "5"
    assert seen == {"left": 3, "right": 2}


def test_context_is_injected_and_not_exposed_in_schema():
    @agent_tool(permissions=_allow)
    def current_call(value: str, ctx: ToolContext) -> str:
        return f"{value}:{ctx.current_call_id}"

    assert "ctx" not in current_call.parameters["properties"]
    registry = ToolRegistry()
    registry.register(current_call)
    result = registry.execute("current_call", {"value": "ok"}, ToolContext(), call_id="c-1")
    assert result.output == "ok:c-1"


def test_default_permission_is_conservative_and_prevents_execution():
    calls = []

    @agent_tool
    def effect(value: str) -> str:
        calls.append(value)
        return value

    registry = ToolRegistry()
    registry.register(effect)
    result = registry.execute("effect", {"value": "x"}, ToolContext(interactive=False))
    assert result.code == "permission_denied"
    assert result.executed is False
    assert calls == []


def test_permission_resolver_still_uses_registry_policy():
    calls = []

    def write_permission(args, _ctx):
        return [
            PermissionRequest(
                "write_record",
                Capability.FILESYSTEM_WRITE,
                args["path"],
                "会修改文件",
            )
        ]

    @agent_tool(permissions=write_permission)
    def write_record(path: str) -> str:
        calls.append(path)
        return "done"

    registry = ToolRegistry()
    registry.register(write_record)
    ctx = ToolContext(permission_policy=PermissionPolicy(mode="readonly"), interactive=False)
    result = registry.execute("write_record", {"path": "outside.txt"}, ctx)
    assert result.code == "permission_denied"
    assert calls == []


def test_registry_validation_and_budget_happen_before_function():
    calls = []

    @agent_tool(permissions=_allow)
    def count(value: int) -> str:
        calls.append(value)
        return str(value)

    registry = ToolRegistry()
    registry.register(count)
    invalid = registry.execute("count", {"value": "bad"}, ToolContext())
    exhausted = registry.execute(
        "count",
        {"value": 1},
        ToolContext(budget=ToolBudget(max_calls=1, used_calls=1)),
    )
    assert invalid.code == "invalid_arguments" and invalid.executed is False
    assert exhausted.code == "budget_exhausted" and exhausted.executed is False
    assert calls == []


def test_result_passthrough_exception_and_invalid_return_are_normalized():
    @agent_tool(permissions=_allow)
    def structured() -> ToolResult:
        return ToolResult.ok("ok", metadata={"source": "function"})

    @agent_tool(permissions=_allow)
    def broken() -> str:
        raise RuntimeError("boom")

    @agent_tool(permissions=_allow)
    def invalid() -> int:
        return 42

    ctx = ToolContext()
    assert structured.run({}, ctx).metadata == {"source": "function"}
    broken_result = broken.run({}, ctx)
    assert broken_result.code == "tool_exception"
    assert "boom" not in broken_result.output
    assert invalid.run({}, ctx).code == "invalid_tool_result"


def test_async_function_fails_at_decoration():
    async def asynchronous(value: str) -> str:
        return value

    with pytest.raises(TypeError, match="仅支持同步 Tool"):
        agent_tool(asynchronous)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def bad(value): return str(value)", "缺少类型注解"),
        ("def bad(*values: int): return str(values)", "不支持可变参数"),
        ("def bad(value: int, /): return str(value)", "不支持位置专用参数"),
        ("def bad(ctx: str): return ctx", "ctx 必须注解为 ToolContext"),
        ("def bad(context: ToolContext): return 'x'", "必须命名为 ctx"),
    ],
)
def test_unsupported_signatures_fail_at_decoration(source, message):
    namespace = {"ToolContext": ToolContext}
    exec(source, namespace)
    with pytest.raises(TypeError, match=message):
        agent_tool(namespace["bad"])
