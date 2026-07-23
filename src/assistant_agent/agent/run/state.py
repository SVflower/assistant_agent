"""可恢复执行的数据契约与稳定标识。

这些 Pydantic 模型是落盘 checkpoint 的权威事实，不是 CLI 的 loading 状态。字段使用 strict/forbid
验证，目的是让损坏或未来版本数据 fail closed，而不是静默猜测后继续执行副作用。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.contracts.charts import ChartArtifactV2
from assistant_agent.contracts.errors import UnsupportedRunStateSchemaError
from assistant_agent.contracts.failures import BudgetResource, RunFailure
from assistant_agent.contracts.time import utc_now_rfc3339

RunStatus = Literal["running", "paused", "cancelled", "completed", "failed"]
RunPhase = Literal[
    "model_pending",
    "tools_pending",
    "awaiting_approval",
    "tool_uncertain",
    "terminal",
]
ToolCallStatus = Literal[
    "planned",
    "awaiting_approval",
    "started",
    "completed",
    "failed",
    "skipped",
]
ReplayPolicy = Literal["safe_readonly", "safe_idempotent", "requires_decision"]
RetrySafety = Literal["safe", "unsafe", "uncertain", "unknown"]

_RESOLVED_TOOL_STATUSES = {"completed", "failed", "skipped"}
_SCHEMA_VERSION: Literal[8] = 8


def now_iso() -> str:
    return utc_now_rfc3339()


def new_run_id() -> str:
    """生成可读、可排序且满足存储路径约束的运行 ID。"""
    return f"run-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def stable_call_id(run_id: str, iteration: int, index: int) -> str:
    """为缺失或冲突的 provider call ID 生成确定性替代值。"""
    seed = f"{run_id}:{iteration}:{index}".encode()
    return f"call-{hashlib.sha256(seed).hexdigest()[:16]}"


def canonical_hash(value: Any) -> str:
    """对 JSON 数据生成与 key 顺序无关的 SHA-256 指纹。"""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class StrictStateModel(BaseModel):
    # strict 禁止诸如 "1" 自动变成 1；extra=forbid 禁止未知字段被悄悄丢掉。
    # 对普通表单这可能显得严格，但恢复状态必须精确，否则错误迁移会改变执行语义。
    model_config = ConfigDict(extra="forbid", strict=True)


class PermissionGrantState(StrictStateModel):
    capability: str
    tool: str
    target: str


class ToolBudgetState(StrictStateModel):
    max_calls: int = Field(ge=1)
    max_total_output_chars: int = Field(ge=0)
    used_calls: int = Field(ge=0)
    used_output_chars: int = Field(ge=0)

    @model_validator(mode="after")
    def _usage_is_consistent(self) -> ToolBudgetState:
        if self.used_calls > self.max_calls:
            raise ValueError("工具调用用量不能超过上限")
        if self.max_total_output_chars > 0 and self.used_output_chars > self.max_total_output_chars:
            raise ValueError("工具输出用量不能超过上限")
        return self


class ContinuationBudgetState(StrictStateModel):
    resource: BudgetResource
    increment: int = Field(gt=0)
    hard_limit: int = Field(gt=0)
    extension_count: int = Field(default=0, ge=0)
    max_extensions: int = Field(default=2, ge=0)


class ContinuationDecisionState(StrictStateModel):
    request_id: str = Field(min_length=1)
    resource: BudgetResource
    old_limit: int = Field(ge=0)
    new_limit: int = Field(ge=0)
    continued: bool


class ToolResultState(StrictStateModel):
    output: str
    is_error: bool
    code: str
    retryable: bool = False
    executed: bool = True
    budget_exhausted: str | None = None
    chart: ChartArtifactV2 | None = None


class PermissionRequestState(StrictStateModel):
    tool: str
    capability: str
    target: str
    risk: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallState(StrictStateModel):
    """一个模型工具调用在 checkpoint 中的生命周期记录。

    `started` 表示真实副作用可能已经发生；只有 resolved 状态才允许携带 result。进程若停在 started，
    恢复逻辑必须结合 replay_policy 决定自动重试还是请求用户处理未知副作用。
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any]
    status: ToolCallStatus = "planned"
    replay_policy: ReplayPolicy = "requires_decision"
    permission_requests: list[PermissionRequestState] = Field(default_factory=list)
    result: ToolResultState | None = None

    @model_validator(mode="after")
    def _result_matches_status(self) -> ToolCallState:
        resolved = self.status in _RESOLVED_TOOL_STATUSES
        if resolved != (self.result is not None):
            raise ValueError("已结束工具状态必须有结果，未结束状态不得带结果")
        return self


class RunState(StrictStateModel):
    """一次 Run 可跨进程恢复的完整状态快照。

    `messages` 是当前执行上下文，`baseline_messages` 是安全重试基线；`session_synced` 只有在终态事实
    成功写入 Session 后才能置真。三者用途不同，不能为了减少字段而互相重建。
    """

    schema_version: Literal[8] = _SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    session_id: str | None = None
    task: str
    status: RunStatus = "running"
    phase: RunPhase = "model_pending"
    interactive: bool
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    system_prompt_hash: str = Field(min_length=64, max_length=64)
    tool_schema_hash: str = Field(min_length=64, max_length=64)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    compaction_checkpoint: dict[str, Any] | None = None
    baseline_messages: list[dict[str, Any]] = Field(default_factory=list)
    baseline_compaction_checkpoint: dict[str, Any] | None = None
    retry_baseline_available: bool = True
    iteration: int = Field(default=0, ge=0)
    iteration_budget: int = Field(ge=1)
    tool_budget: ToolBudgetState
    iteration_continuation: ContinuationBudgetState = Field(
        default_factory=lambda: ContinuationBudgetState(
            resource="iterations", increment=25, hard_limit=100
        )
    )
    tool_call_continuation: ContinuationBudgetState = Field(
        default_factory=lambda: ContinuationBudgetState(
            resource="tool_calls", increment=50, hard_limit=200
        )
    )
    tool_output_continuation: ContinuationBudgetState = Field(
        default_factory=lambda: ContinuationBudgetState(
            resource="tool_output", increment=50_000, hard_limit=400_000
        )
    )
    continuation_decisions: list[ContinuationDecisionState] = Field(default_factory=list)
    last_signature: str | None = None
    repeat_count: int = Field(default=0, ge=0)
    tool_calls: list[ToolCallState] = Field(default_factory=list)
    presentations: list[ChartArtifactV2] = Field(default_factory=list, max_length=16)
    permission_grants: list[PermissionGrantState] = Field(default_factory=list)
    terminal_text: str = ""
    failure: RunFailure | None = None
    retry_safety: RetrySafety = "safe"
    retry_of_run_id: str | None = None
    retry_idempotency_key_hash: str | None = Field(default=None, min_length=64, max_length=64)
    retry_request_hash: str | None = Field(default=None, min_length=64, max_length=64)
    reconciliation_request_hash: str | None = Field(default=None, min_length=64, max_length=64)
    retry_requests: dict[str, str] = Field(default_factory=dict)
    session_synced: bool = False
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _state_is_consistent(self) -> RunState:
        is_terminal = self.status in {"cancelled", "completed", "failed"}
        if is_terminal != (self.phase == "terminal"):
            raise ValueError("completed/failed 与 terminal phase 必须一致")
        if is_terminal and any(
            call.status not in _RESOLVED_TOOL_STATUSES for call in self.tool_calls
        ):
            raise ValueError("terminal Run 不能保留未结束工具调用")
        if self.status == "failed" and (
            self.failure is None or self.failure.terminal_status != "failed"
        ):
            raise ValueError("failed Run 必须保存结构化 failure")
        if self.status == "paused" and self.failure is not None:
            if self.failure.terminal_status != "paused":
                raise ValueError("paused Run failure 必须标记 paused")
        if self.status not in {"failed", "paused"} and self.failure is not None:
            raise ValueError("只有 failed/paused Run 可以保存 failure")
        if self.iteration_continuation.hard_limit < self.iteration_budget:
            raise ValueError("iteration continuation 硬上限不能小于当前预算")
        if self.tool_call_continuation.hard_limit < self.tool_budget.max_calls:
            raise ValueError("tool call continuation 硬上限不能小于当前预算")
        if (
            self.tool_budget.max_total_output_chars > 0
            and self.tool_output_continuation.hard_limit < self.tool_budget.max_total_output_chars
        ):
            raise ValueError("tool output continuation 硬上限不能小于当前预算")
        if self.repeat_count and self.last_signature is None:
            raise ValueError("repeat_count 非零时必须保存 last_signature")
        ids = [call.id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("当前工具批次存在重复 call ID")
        artifact_ids = [item.artifact_id for item in self.presentations]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("Run 存在重复 artifact_id")
        if any(item.run_id != self.run_id for item in self.presentations):
            raise ValueError("Artifact 不属于当前 Run")
        if any(item.session_id != self.session_id for item in self.presentations):
            raise ValueError("Artifact 不属于当前 Session")
        if sum(item.size_bytes for item in self.presentations) > 2 * 1024 * 1024:
            raise ValueError("Run Artifact 总量超过 2 MiB")
        retry_hashes = (self.retry_idempotency_key_hash, self.retry_request_hash)
        if (retry_hashes[0] is None) != (retry_hashes[1] is None):
            raise ValueError("重试幂等键哈希与请求哈希必须同时存在")
        if self.retry_of_run_id is None and any(item is not None for item in retry_hashes):
            raise ValueError("只有重试创建的 Run 可以保存重试请求哈希")
        if any(len(key) != 64 or not run_id for key, run_id in self.retry_requests.items()):
            raise ValueError("重试幂等记录无效")
        self._validate_tool_messages()
        return self

    def _validate_tool_messages(self) -> None:
        assistant_ids: set[str] = set()
        result_ids: set[str] = set()
        for message in self.messages:
            for raw_call in message.get("tool_calls") or []:
                call_id = raw_call.get("id") if isinstance(raw_call, dict) else None
                if isinstance(call_id, str):
                    assistant_ids.add(call_id)
            if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str):
                result_ids.add(message["tool_call_id"])

        for call in self.tool_calls:
            if call.id not in assistant_ids:
                raise ValueError(f"工具状态缺少 assistant tool_call：{call.id}")
            has_message = call.id in result_ids
            is_resolved = call.status in _RESOLVED_TOOL_STATUSES
            if has_message != is_resolved:
                raise ValueError(f"工具状态与 tool result 消息不配对：{call.id}")


def parse_run_state(document: dict[str, Any]) -> RunState:
    """只接受当前 checkpoint schema，不补齐或转换旧状态。"""
    version = document.get("schema_version")
    if version != _SCHEMA_VERSION:
        raise UnsupportedRunStateSchemaError(
            f"Run checkpoint schema 不兼容：需要 v{_SCHEMA_VERSION}",
            expected_version=_SCHEMA_VERSION,
            actual_version=version,
        )
    return RunState.model_validate(document)
