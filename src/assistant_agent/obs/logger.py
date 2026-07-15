"""结构化事件日志（JSON Lines）与工具审计。

一行一个 JSON 事件，追加到 `<dir>/<日期>.jsonl`。事件含 ts/trace_id/type +
该事件字段。写入非致命：任何异常都吞掉，绝不因日志写不了而中断任务
（对齐既有"自动保存失败只警告"原则）。

隐私：参数/输出可能含敏感信息 → 落盘前做尽力而为的脱敏 + 截断。
脱敏非保证；日志仅本地、随 .assistant_agent/ gitignore 不入库，并提供
log_tool_io=false 一键只记元数据。

NullLogger 为默认注入值（禁用/未配置时零副作用），也用于测试。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from assistant_agent.obs.redaction import redact_text, sanitize_args, truncate_text

if TYPE_CHECKING:
    from assistant_agent.config.schema import LoggingConfig


def new_trace_id() -> str:
    return f"trace-{secrets.token_hex(12)}"


class NullLogger:
    """无操作日志器：所有方法什么都不做。

    作为 ToolContext.logger 的默认值——不配置/关闭日志时零副作用，测试也用它。
    同时充当"接口定义"：EventLogger 覆盖这些方法给出真正实现。
    """

    def session_start(self, *, provider: str, model: str, mode: str, cwd: str) -> None: ...

    def session_end(self, *, reason: str = "") -> None: ...

    def bind_session(self, session_id: str | None) -> None: ...

    def run_start(self, *, run_id: str, provider: str, model: str, task: str) -> None: ...

    def run_resume(
        self,
        *,
        run_id: str,
        phase: str,
        source: str,
        provider: str,
        model: str,
        warning: str = "",
    ) -> None: ...

    def run_checkpoint(self, *, run_id: str, status: str, phase: str, iteration: int) -> None: ...

    def run_end(self, *, run_id: str, status: str, reason: str = "") -> None: ...

    def model_switch(
        self, *, from_provider: str, from_model: str, to_provider: str, to_model: str
    ) -> None: ...

    def task(self, text: str) -> None: ...

    def tool_call(
        self,
        *,
        name: str,
        args: dict[str, Any],
        duration_ms: int,
        status: str,
        output: str,
        approval_wait_ms: int | None = None,
        truncated: bool = False,
        wall_duration_ms: int | None = None,
        execution_duration_ms: int | None = None,
        returned_output_len: int | None = None,
        call_id: str = "",
    ) -> None: ...

    def budget_exhausted(
        self, *, reason: str, limit: int, used: int, skipped_calls: int
    ) -> None: ...

    def confirm(self, *, category: str, decision: str, remembered: bool) -> None: ...

    def permission_decision(
        self,
        *,
        mode: str,
        tool: str,
        capabilities: list[str],
        targets: list[str],
        decision: str,
        reason: str,
        remembered: bool,
        matched_rules: list[str],
    ) -> None: ...

    def observer_error(self, *, phase: str, tool: str, error: str) -> None: ...


class EventLogger(NullLogger):
    """把事件按 JSONL 追加到按天分卷的日志文件。"""

    def __init__(
        self,
        log_dir: str | Path,
        trace_id: str,
        *,
        session_id: str | None | object = ...,
        log_tool_io: bool = True,
        max_payload_chars: int = 2000,
    ) -> None:
        self._dir = Path(log_dir)
        self._trace_id = trace_id
        self._session_id = trace_id if session_id is ... else session_id
        self._run_id: str | None = None
        self._provider = ""
        self._model = ""
        self._log_tool_io = log_tool_io
        self._max_chars = max_payload_chars

    def _path(self) -> Path:
        """当天日志文件路径（按天分卷，简单有界）。"""
        return self._dir / f"{datetime.now():%Y-%m-%d}.jsonl"

    def _write(self, event: dict[str, Any]) -> None:
        """补齐关联标识并追加一行。写入失败非致命，静默吞掉。"""
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "trace_id": self._trace_id,
            **event,
        }
        if self._session_id is not None:
            record["session_id"] = self._session_id
        if self._run_id is not None:
            record["run_id"] = self._run_id
        if self._provider:
            record["provider"] = self._provider
        if self._model:
            record["model"] = self._model
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str)
            # errors="replace"：模型输出偶含无法编码字符，日志绝不能因此崩。
            with self._path().open("ab") as f:
                f.write((line + "\n").encode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - 日志尽力而为，任何异常都不该影响任务
            pass

    def session_start(self, *, provider: str, model: str, mode: str, cwd: str) -> None:
        self._write(
            {
                "type": "session_start",
                "provider": provider,
                "model": model,
                "mode": mode,
                "cwd": cwd,
            }
        )

    def session_end(self, *, reason: str = "") -> None:
        self._write({"type": "session_end", "reason": reason})

    def bind_session(self, session_id: str | None) -> None:
        self._session_id = session_id

    def run_start(self, *, run_id: str, provider: str, model: str, task: str) -> None:
        self._run_id = run_id
        self._provider = provider
        self._model = model
        self._write({"type": "run_start", "task": truncate_text(task, self._max_chars)})

    def run_resume(
        self,
        *,
        run_id: str,
        phase: str,
        source: str,
        provider: str,
        model: str,
        warning: str = "",
    ) -> None:
        self._run_id = run_id
        self._provider = provider
        self._model = model
        self._write(
            {
                "type": "run_resume",
                "phase": phase,
                "source": source,
                "warning": warning,
            }
        )

    def run_checkpoint(self, *, run_id: str, status: str, phase: str, iteration: int) -> None:
        self._run_id = run_id
        self._write(
            {
                "type": "run_checkpoint",
                "status": status,
                "phase": phase,
                "iteration": iteration,
            }
        )

    def run_end(self, *, run_id: str, status: str, reason: str = "") -> None:
        self._run_id = run_id
        self._write({"type": "run_end", "status": status, "reason": reason})

    def model_switch(
        self, *, from_provider: str, from_model: str, to_provider: str, to_model: str
    ) -> None:
        self._write(
            {
                "type": "model_switch",
                "from_provider": from_provider,
                "from_model": from_model,
                "to_provider": to_provider,
                "to_model": to_model,
            }
        )
        self._provider = to_provider
        self._model = to_model

    def task(self, text: str) -> None:
        self._write({"type": "task", "text": truncate_text(text, self._max_chars)})

    def tool_call(
        self,
        *,
        name: str,
        args: dict[str, Any],
        duration_ms: int,
        status: str,
        output: str,
        approval_wait_ms: int | None = None,
        truncated: bool = False,
        wall_duration_ms: int | None = None,
        execution_duration_ms: int | None = None,
        returned_output_len: int | None = None,
        call_id: str = "",
    ) -> None:
        execution_ms = duration_ms if execution_duration_ms is None else execution_duration_ms
        event: dict[str, Any] = {
            "type": "tool_call",
            "tool": name,
            "duration_ms": execution_ms,
            "execution_duration_ms": execution_ms,
            "wall_duration_ms": wall_duration_ms if wall_duration_ms is not None else duration_ms,
            "status": status,
            "output_len": len(output),
            "returned_output_len": (
                len(output) if returned_output_len is None else returned_output_len
            ),
        }
        # 仅在确实等过用户确认时才记录，避免给绝大多数工具调用增噪。
        if approval_wait_ms is not None:
            event["approval_wait_ms"] = approval_wait_ms
        if call_id:
            event["call_id"] = call_id
        # 写入上下文的输出被截断时标记（output_len 仍是原始长度）。
        if truncated:
            event["truncated"] = True
        if self._log_tool_io:
            event["args"] = sanitize_args(args, self._max_chars)
            event["output"] = truncate_text(redact_text(output), self._max_chars)
        self._write(event)

    def budget_exhausted(self, *, reason: str, limit: int, used: int, skipped_calls: int) -> None:
        self._write(
            {
                "type": "budget_exhausted",
                "reason": reason,
                "limit": limit,
                "used": used,
                "skipped_calls": skipped_calls,
            }
        )

    def confirm(self, *, category: str, decision: str, remembered: bool) -> None:
        self._write(
            {
                "type": "confirm",
                "category": category,
                "decision": decision,
                "remembered": remembered,
            }
        )

    def permission_decision(
        self,
        *,
        mode: str,
        tool: str,
        capabilities: list[str],
        targets: list[str],
        decision: str,
        reason: str,
        remembered: bool,
        matched_rules: list[str],
    ) -> None:
        self._write(
            {
                "type": "permission_decision",
                "mode": mode,
                "tool": tool,
                "capabilities": capabilities,
                "targets": [
                    truncate_text(redact_text(target), self._max_chars) for target in targets
                ],
                "decision": decision,
                "reason": reason,
                "remembered": remembered,
                "matched_rules": matched_rules,
            }
        )

    def observer_error(self, *, phase: str, tool: str, error: str) -> None:
        self._write(
            {
                "type": "observer_error",
                "phase": phase,
                "tool": tool,
                "error": truncate_text(redact_text(error), self._max_chars),
            }
        )


def create_logger(
    cfg: LoggingConfig,
    trace_id: str,
    *,
    session_id: str | None | object = ...,
) -> NullLogger:
    """按配置构建日志器：禁用时返回 NullLogger（零副作用），否则 EventLogger。"""
    if not cfg.enabled:
        return NullLogger()
    return EventLogger(
        cfg.dir,
        trace_id,
        session_id=session_id,
        log_tool_io=cfg.log_tool_io,
        max_payload_chars=cfg.max_payload_chars,
    )
