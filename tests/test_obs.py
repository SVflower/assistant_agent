"""可观测性层测试：结构化事件日志、脱敏截断、两个落点。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from assistant_agent.obs import EventLogger, NullLogger
from assistant_agent.tools.base import Tool, ToolBudget, ToolContext, ToolResult
from assistant_agent.tools.registry import ToolRegistry, build_default_registry


def _read_events(log_dir: Path) -> list[dict]:
    files = list(Path(log_dir).glob("*.jsonl"))
    assert len(files) == 1, f"应恰好一个日志文件，实际 {files}"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _logger(log_dir: Path, **kwargs) -> EventLogger:
    return EventLogger(log_dir, "sid-test", **kwargs)


# ---- EventLogger 基础 ----


def test_event_logger_writes_jsonl(tmp_path):
    logger = _logger(tmp_path)
    logger.session_start(provider="p", model="m", mode="run", cwd="/x")
    logger.tool_call(name="read_file", args={"path": "/a"}, duration_ms=5, status="ok", output="hi")

    events = _read_events(tmp_path)
    assert len(events) == 2
    for e in events:
        assert e["ts"] and e["session_id"] == "sid-test" and "type" in e
    start, call = events
    assert start["type"] == "session_start" and start["provider"] == "p"
    assert call["type"] == "tool_call" and call["tool"] == "read_file"
    assert call["duration_ms"] == 5 and call["status"] == "ok" and call["output_len"] == 2
    assert call["wall_duration_ms"] == 5
    assert call["execution_duration_ms"] == 5
    assert call["returned_output_len"] == 2


def test_null_logger_writes_nothing(tmp_path):
    logger = NullLogger()
    logger.session_start(provider="p", model="m", mode="run", cwd="/x")
    logger.tool_call(name="t", args={}, duration_ms=0, status="ok", output="")
    logger.confirm(category="c", decision="allow", remembered=False)
    logger.task("hi")
    logger.session_end()
    assert not list(tmp_path.glob("*.jsonl"))


# ---- 脱敏与截断 ----


def test_redact_secret_key_name(tmp_path):
    logger = _logger(tmp_path)
    logger.tool_call(
        name="t", args={"api_key": "sk-abc123", "path": "/a"}, duration_ms=1, status="ok", output=""
    )
    args = _read_events(tmp_path)[0]["args"]
    assert args["api_key"] == "***REDACTED***"
    assert args["path"] == "/a"  # 非敏感键原样


def test_redact_secret_value_in_string(tmp_path):
    logger = _logger(tmp_path)
    logger.tool_call(
        name="run_shell",
        args={"command": "export T=sk-abcdefghijkl"},
        duration_ms=1,
        status="ok",
        output="",
    )
    dumped = json.dumps(_read_events(tmp_path)[0]["args"], ensure_ascii=False)
    assert "sk-abcdefghijkl" not in dumped
    assert "REDACTED" in dumped


def test_redact_nested_secret_in_list_of_dicts(tmp_path):
    """D8②：嵌套结构里的密钥也要脱敏（multi_edit.edits[].new_string 场景）。"""
    logger = _logger(tmp_path)
    logger.tool_call(
        name="multi_edit",
        args={
            "path": "/a",
            "edits": [
                {"old_string": "x", "new_string": "TOKEN=ghp_abcdefghijklmnop1234"},
            ],
        },
        duration_ms=1,
        status="ok",
        output="",
    )
    dumped = json.dumps(_read_events(tmp_path)[0]["args"], ensure_ascii=False)
    assert "ghp_abcdefghijklmnop1234" not in dumped  # 嵌套密钥被遮蔽
    assert "REDACTED" in dumped


def test_redact_nested_secret_key_name(tmp_path):
    """嵌套 dict 里的敏感键名同样整体遮蔽。"""
    logger = _logger(tmp_path)
    logger.tool_call(
        name="t",
        args={"config": {"api_token": "raw-secret-value", "host": "localhost"}},
        duration_ms=1,
        status="ok",
        output="",
    )
    cfg = _read_events(tmp_path)[0]["args"]["config"]
    assert cfg["api_token"] == "***REDACTED***"
    assert cfg["host"] == "localhost"  # 非敏感键原样


def test_truncate_payload(tmp_path):
    logger = _logger(tmp_path, max_payload_chars=10)
    logger.tool_call(name="t", args={}, duration_ms=1, status="ok", output="x" * 100)
    event = _read_events(tmp_path)[0]
    assert event["output_len"] == 100  # 原始长度
    assert "…(+90 chars)" in event["output"]  # 载荷被截断


def test_log_tool_io_false_omits_payload(tmp_path):
    logger = _logger(tmp_path, log_tool_io=False)
    logger.tool_call(name="t", args={"secret": "x"}, duration_ms=7, status="ok", output="body")
    event = _read_events(tmp_path)[0]
    assert "args" not in event and "output" not in event
    assert event["duration_ms"] == 7 and event["status"] == "ok" and event["output_len"] == 4


def test_write_failure_is_non_fatal(tmp_path):
    # 让日志目录不可创建（父路径是个文件），写入应静默失败、不抛。
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    logger = _logger(blocker / "logs")
    logger.tool_call(name="t", args={}, duration_ms=1, status="ok", output="")  # 不应抛


# ---- 落点：registry.execute ----


def test_registry_execute_logs_tool_call_ok(tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("hi", encoding="utf-8")
    ctx = ToolContext(logger=_logger(tmp_path / "logs"), workspace_root=tmp_path)
    result = build_default_registry().execute("read_file", {"path": str(f)}, ctx)
    assert not result.is_error

    events = _read_events(tmp_path / "logs")
    assert [event["type"] for event in events] == ["permission_decision", "tool_call"]
    assert events[0]["matched_rules"] == []
    e = events[-1]
    assert e["type"] == "tool_call" and e["tool"] == "read_file" and e["status"] == "ok"
    assert e["duration_ms"] >= 0


def test_registry_execute_logs_error_status(tmp_path):
    ctx = ToolContext(logger=_logger(tmp_path / "logs"))
    result = build_default_registry().execute("read_file", {"path": 123}, ctx)
    assert result.is_error
    assert _read_events(tmp_path / "logs")[0]["status"] == "error"


def test_registry_unknown_tool_not_logged(tmp_path):
    """未知工具没执行 → 不产生 tool_call 事件（提前 return）。"""
    ctx = ToolContext(logger=_logger(tmp_path / "logs"))
    build_default_registry().execute("nonexistent", {}, ctx)
    assert not list((tmp_path / "logs").glob("*.jsonl"))


# ---- 落点：request_confirm 审计 ----


def test_request_confirm_audits_all_decisions(tmp_path):
    # allow / deny
    for choice in ("allow", "deny"):
        d = tmp_path / choice
        ctx = ToolContext(confirm=lambda _m, c=choice: c, logger=_logger(d))
        ctx.request_confirm("run_shell", "msg")
        e = _read_events(d)[0]
        assert e["type"] == "confirm" and e["decision"] == choice and e["remembered"] is False

    # always：记 decision=always，并进入永久允许记忆
    d = tmp_path / "always"
    ctx = ToolContext(confirm=lambda _m: "always", logger=_logger(d))
    ctx.request_confirm("run_shell", "msg")
    assert _read_events(d)[0]["decision"] == "always"
    assert "run_shell" in ctx.always_allowed


def test_request_confirm_remembered_allow(tmp_path):
    """命中永久允许记忆 → decision=allow, remembered=True，且不再调 confirm 回调。"""
    called = {"n": 0}

    def confirm(_m: str) -> str:
        called["n"] += 1
        return "deny"

    ctx = ToolContext(confirm=confirm, always_allowed={"run_shell"}, logger=_logger(tmp_path))
    assert ctx.request_confirm("run_shell", "msg") is True
    assert called["n"] == 0
    e = _read_events(tmp_path)[0]
    assert e["decision"] == "allow" and e["remembered"] is True


# ---- D8① 确认等待时间与执行耗时分离 ----


class _ConfirmingTool(Tool):
    """测试用：run 里请求确认，确认回调会 sleep 模拟"等人"。"""

    name = "confirming"
    description = "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        ctx.request_confirm("run_shell", "确认？")
        return ToolResult.ok("done")

    def permission_requests(self, args, ctx):
        return []


def _registry_with(tool: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool)
    return reg


def test_approval_wait_separated_from_duration(tmp_path):
    """确认等待应从 duration_ms 剥离，并单列 approval_wait_ms。"""

    def slow_confirm(_m: str) -> str:
        time.sleep(0.05)  # 模拟等人 ~50ms
        return "allow"

    ctx = ToolContext(confirm=slow_confirm, logger=_logger(tmp_path / "logs"))
    _registry_with(_ConfirmingTool()).execute("confirming", {}, ctx)

    e = next(x for x in _read_events(tmp_path / "logs") if x["type"] == "tool_call")
    assert e["approval_wait_ms"] >= 40  # 记录了等待（宽松下界，避免 flaky）
    assert e["duration_ms"] < e["approval_wait_ms"]  # 执行耗时已剥离等待
    assert e["wall_duration_ms"] >= e["approval_wait_ms"]
    assert e["execution_duration_ms"] == e["duration_ms"]
    # 用后即清，不残留到下次调用
    assert ctx.consume_approval_wait() == 0


class _DoubleConfirmingTool(Tool):
    name = "double_confirming"
    description = "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        ctx.request_confirm("first", "确认 1？")
        ctx.request_confirm("second", "确认 2？")
        return ToolResult.ok("done")

    def permission_requests(self, args, ctx):
        return []


def test_approval_wait_accumulates_multiple_confirms(tmp_path):
    def slow_confirm(_m: str) -> str:
        time.sleep(0.03)
        return "allow"

    ctx = ToolContext(confirm=slow_confirm, logger=_logger(tmp_path / "logs"))
    _registry_with(_DoubleConfirmingTool()).execute("double_confirming", {}, ctx)
    event = next(e for e in _read_events(tmp_path / "logs") if e["type"] == "tool_call")
    assert event["approval_wait_ms"] >= 50


def test_no_confirm_omits_approval_wait(tmp_path):
    """普通工具（不请求确认）的事件不含 approval_wait_ms。"""
    f = tmp_path / "n.txt"
    f.write_text("hi", encoding="utf-8")
    ctx = ToolContext(logger=_logger(tmp_path / "logs"), workspace_root=tmp_path)
    build_default_registry().execute("read_file", {"path": str(f)}, ctx)
    e = _read_events(tmp_path / "logs")[0]
    assert "approval_wait_ms" not in e


# ---- S5 工具单次输出截断 ----


class _BigOutputTool(Tool):
    name = "big"
    description = "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("x" * 100)

    def permission_requests(self, args, ctx):
        return []


def test_output_truncated_when_over_limit(tmp_path):
    """超限：返回给上下文的 output 被截断+标记，日志记原始长度并标 truncated。"""
    ctx = ToolContext(logger=_logger(tmp_path / "logs"), max_output_chars=40)
    result = _registry_with(_BigOutputTool()).execute("big", {}, ctx)
    assert len(result.output) == 40
    assert "输出已截断" in result.output
    e = _read_events(tmp_path / "logs")[0]
    assert e["output_len"] == 100 and e["truncated"] is True
    assert e["returned_output_len"] == 40


def test_output_not_truncated_under_limit(tmp_path):
    """未超限：output 原样，日志不含 truncated。"""
    ctx = ToolContext(logger=_logger(tmp_path / "logs"), max_output_chars=1000)
    result = _registry_with(_BigOutputTool()).execute("big", {}, ctx)
    assert result.output == "x" * 100
    assert "truncated" not in _read_events(tmp_path / "logs")[0]


def test_output_limit_zero_disables_truncation(tmp_path):
    """默认 0：不截断（兼容裸 ToolContext）。"""
    ctx = ToolContext(logger=_logger(tmp_path / "logs"))  # max_output_chars 默认 0
    result = _registry_with(_BigOutputTool()).execute("big", {}, ctx)
    assert result.output == "x" * 100


def test_registry_call_budget_blocks_execution(tmp_path):
    budget = ToolBudget(max_calls=1, max_total_output_chars=1000)
    ctx = ToolContext(logger=_logger(tmp_path / "logs"), budget=budget)
    registry = _registry_with(_BigOutputTool())

    assert not registry.execute("big", {}, ctx).is_error
    blocked = registry.execute("big", {}, ctx)

    assert blocked.is_error
    assert blocked.budget_exhausted == "max_tool_calls"
    assert budget.used_calls == 1


def test_registry_total_output_budget_truncates_and_exhausts(tmp_path):
    budget = ToolBudget(max_calls=3, max_total_output_chars=40)
    ctx = ToolContext(logger=_logger(tmp_path / "logs"), max_output_chars=100, budget=budget)
    result = _registry_with(_BigOutputTool()).execute("big", {}, ctx)

    assert len(result.output) == 40
    assert result.budget_exhausted == "max_total_tool_output_chars"
    assert budget.used_output_chars == 40


def test_unknown_tool_does_not_consume_call_budget(tmp_path):
    budget = ToolBudget(max_calls=1)
    ctx = ToolContext(logger=_logger(tmp_path / "logs"), budget=budget)
    result = build_default_registry().execute("missing", {}, ctx)

    assert result.is_error
    assert budget.used_calls == 0
    assert budget.used_output_chars == len(result.output)


def test_budget_exhausted_event(tmp_path):
    logger = _logger(tmp_path)
    logger.budget_exhausted(
        reason="max_tool_calls",
        limit=5,
        used=5,
        skipped_calls=2,
    )

    event = _read_events(tmp_path)[0]
    assert event == {
        "ts": event["ts"],
        "session_id": "sid-test",
        "type": "budget_exhausted",
        "reason": "max_tool_calls",
        "limit": 5,
        "used": 5,
        "skipped_calls": 2,
    }
