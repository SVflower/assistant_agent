"""可观测性层测试：结构化事件日志、脱敏截断、两个落点。"""

from __future__ import annotations

import json
from pathlib import Path

from assistant_agent.obs import EventLogger, NullLogger
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import build_default_registry


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


def test_truncate_payload(tmp_path):
    logger = _logger(tmp_path, max_payload_chars=10)
    logger.tool_call(name="t", args={}, duration_ms=1, status="ok", output="x" * 100)
    event = _read_events(tmp_path)[0]
    assert event["output_len"] == 100  # 原始长度
    assert "…(+90 chars)" in event["output"]  # 载荷被截断


def test_log_tool_io_false_omits_payload(tmp_path):
    logger = _logger(tmp_path, log_tool_io=False)
    logger.tool_call(
        name="t", args={"secret": "x"}, duration_ms=7, status="ok", output="body"
    )
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
    ctx = ToolContext(logger=_logger(tmp_path / "logs"))
    result = build_default_registry().execute("read_file", {"path": str(f)}, ctx)
    assert not result.is_error

    events = _read_events(tmp_path / "logs")
    assert len(events) == 1
    e = events[0]
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

    ctx = ToolContext(
        confirm=confirm, always_allowed={"run_shell"}, logger=_logger(tmp_path)
    )
    assert ctx.request_confirm("run_shell", "msg") is True
    assert called["n"] == 0
    e = _read_events(tmp_path)[0]
    assert e["decision"] == "allow" and e["remembered"] is True
