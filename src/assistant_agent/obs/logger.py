"""结构化事件日志（JSON Lines）与工具审计。

一行一个 JSON 事件，追加到 `<dir>/<日期>.jsonl`。事件含 ts/session_id/type +
该事件字段。写入非致命：任何异常都吞掉，绝不因日志写不了而中断任务
（对齐既有"自动保存失败只警告"原则）。

隐私：参数/输出可能含敏感信息 → 落盘前做尽力而为的脱敏 + 截断。
脱敏非保证；日志仅本地、随 .assistant_agent/ gitignore 不入库，并提供
log_tool_io=false 一键只记元数据。

NullLogger 为默认注入值（禁用/未配置时零副作用），也用于测试。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from assistant_agent.config.schema import LoggingConfig

# 疑似敏感的参数键名（小写子串匹配）：命中则整体遮蔽其值。
_SECRET_KEY_HINTS = ("key", "token", "password", "passwd", "secret", "credential", "auth")

# 疑似密钥的值模式：只匹配带已知前缀的形态（sk-/sk-ant-/ghp_/AKIA/xox…），
# 尽力而为、非穷尽。刻意不匹配"任意 32+ 长串"——那会大面积误伤长代码/数据正文。
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})"
)

_REDACTED = "***REDACTED***"


def _redact_str(value: str) -> str:
    """遮蔽字符串里疑似密钥的片段。"""
    return _SECRET_VALUE_RE.sub(_REDACTED, value)


def _truncate(value: str, max_chars: int) -> str:
    """超长截断并标记省略了多少字符。"""
    if max_chars > 0 and len(value) > max_chars:
        return value[:max_chars] + f"…(+{len(value) - max_chars} chars)"
    return value


def _sanitize_args(args: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """对工具参数做脱敏 + 截断：敏感键名整体遮蔽，其余字符串值脱敏并截断。"""
    clean: dict[str, Any] = {}
    for key, val in args.items():
        if any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
            clean[key] = _REDACTED
        elif isinstance(val, str):
            clean[key] = _truncate(_redact_str(val), max_chars)
        else:
            clean[key] = val
    return clean


class NullLogger:
    """无操作日志器：所有方法什么都不做。

    作为 ToolContext.logger 的默认值——不配置/关闭日志时零副作用，测试也用它。
    同时充当"接口定义"：EventLogger 覆盖这些方法给出真正实现。
    """

    def session_start(self, *, provider: str, model: str, mode: str, cwd: str) -> None: ...

    def session_end(self, *, reason: str = "") -> None: ...

    def task(self, text: str) -> None: ...

    def tool_call(
        self,
        *,
        name: str,
        args: dict[str, Any],
        duration_ms: int,
        status: str,
        output: str,
    ) -> None: ...

    def confirm(self, *, category: str, decision: str, remembered: bool) -> None: ...


class EventLogger(NullLogger):
    """把事件按 JSONL 追加到按天分卷的日志文件。"""

    def __init__(
        self,
        log_dir: str | Path,
        session_id: str,
        *,
        log_tool_io: bool = True,
        max_payload_chars: int = 2000,
    ) -> None:
        self._dir = Path(log_dir)
        self._session_id = session_id
        self._log_tool_io = log_tool_io
        self._max_chars = max_payload_chars

    def _path(self) -> Path:
        """当天日志文件路径（按天分卷，简单有界）。"""
        return self._dir / f"{datetime.now():%Y-%m-%d}.jsonl"

    def _write(self, event: dict[str, Any]) -> None:
        """给事件补上 ts/session_id 并追加一行。写入失败非致命，静默吞掉。"""
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "session_id": self._session_id,
            **event,
        }
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

    def task(self, text: str) -> None:
        self._write({"type": "task", "text": _truncate(text, self._max_chars)})

    def tool_call(
        self,
        *,
        name: str,
        args: dict[str, Any],
        duration_ms: int,
        status: str,
        output: str,
    ) -> None:
        event: dict[str, Any] = {
            "type": "tool_call",
            "tool": name,
            "duration_ms": duration_ms,
            "status": status,
            "output_len": len(output),
        }
        if self._log_tool_io:
            event["args"] = _sanitize_args(args, self._max_chars)
            event["output"] = _truncate(_redact_str(output), self._max_chars)
        self._write(event)

    def confirm(self, *, category: str, decision: str, remembered: bool) -> None:
        self._write(
            {
                "type": "confirm",
                "category": category,
                "decision": decision,
                "remembered": remembered,
            }
        )


def create_logger(cfg: LoggingConfig, session_id: str) -> NullLogger:
    """按配置构建日志器：禁用时返回 NullLogger（零副作用），否则 EventLogger。"""
    if not cfg.enabled:
        return NullLogger()
    return EventLogger(
        cfg.dir,
        session_id,
        log_tool_io=cfg.log_tool_io,
        max_payload_chars=cfg.max_payload_chars,
    )
