"""版本化 eval case、轨迹与报告数据契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal[1] = 1


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneratedFileSpec(StrictModel):
    lines: int = Field(gt=0, le=100_000)
    template: str = Field(default="line-{line}\n", max_length=1_000)


class FixtureSpec(StrictModel):
    files: dict[str, str] = Field(default_factory=dict)
    generated_files: dict[str, GeneratedFileSpec] = Field(default_factory=dict)


class EvalPermissionRule(StrictModel):
    effect: Literal["allow", "ask", "deny"]
    capability: Literal[
        "filesystem.read",
        "filesystem.write",
        "process.execute",
        "network.access",
        "mcp.call",
        "skill.load",
        "user.interaction",
    ]
    target: str = "*"
    tool: str = "*"


class EvalPermissions(StrictModel):
    mode: Literal["readonly", "workspace", "strict", "unrestricted"] = "workspace"
    rules: list[EvalPermissionRule] = Field(default_factory=list)
    confirm: Literal["allow", "always", "deny"] = "deny"


class EvalBudget(StrictModel):
    max_iterations: int = Field(default=10, gt=0)
    max_tool_calls: int = Field(default=12, gt=0)
    max_total_tool_output_chars: int = Field(default=20_000, ge=0)
    max_output_chars: int = Field(default=4_000, ge=0)
    max_context_tokens: int = Field(default=8_000, gt=0)
    max_history_messages: int = Field(default=40, gt=0)
    reserved_output_tokens: int = Field(default=1_024, ge=0)
    compaction_enabled: bool = False
    compaction_threshold: float = Field(default=0.8, gt=0, le=1)
    compaction_keep_recent_turns: int = Field(default=2, gt=0)


class ScriptToolCall(StrictModel):
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    id: str = ""


class ScriptRound(StrictModel):
    final: str | None = None
    error: str | None = None
    reasoning: str = ""
    tool_calls: list[ScriptToolCall] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_action(self) -> ScriptRound:
        actions = (
            int(self.final is not None) + int(self.error is not None) + int(bool(self.tool_calls))
        )
        if actions != 1:
            raise ValueError("script 每轮必须且只能声明 final、error 或 tool_calls 之一")
        if any(value < 0 for value in self.usage.values()):
            raise ValueError("usage 不能为负数")
        return self


class FileExpectation(StrictModel):
    exists: bool | None = None
    equals: str | None = None
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> FileExpectation:
        if self.exists is False and (self.equals is not None or self.contains or self.not_contains):
            raise ValueError("exists=false 不能同时声明内容断言")
        return self


class ExpectedToolCall(StrictModel):
    name: str
    arguments: dict[str, Any] | None = None
    output_contains: list[str] = Field(default_factory=list)
    output_not_contains: list[str] = Field(default_factory=list)
    is_error: bool | None = None


class EvalExpectation(StrictModel):
    outcome: Literal["success", "error", "interrupted"] = "success"
    trajectory: Literal["strict", "unordered", "subset", "superset"] = "subset"
    expected_calls: list[ExpectedToolCall] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    final_exact: str | None = None
    final_contains: list[str] = Field(default_factory=list)
    final_not_contains: list[str] = Field(default_factory=list)
    files: dict[str, FileExpectation] = Field(default_factory=dict)
    permission_denials: int | None = Field(default=None, ge=0)


class SkillMock(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    description: str = "eval skill"
    body: str = ""
    source: Literal["project", "personal", "configured"] = "project"
    trusted: bool = False


class MCPMock(StrictModel):
    server: str = "mock"
    tool: str
    result: str = "ok"
    trusted: bool = False


class MockSpec(StrictModel):
    skills: list[SkillMock] = Field(default_factory=list)
    mcp_tools: list[MCPMock] = Field(default_factory=list)


class EvalCase(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    task: str = Field(min_length=1)
    fixture: FixtureSpec = Field(default_factory=FixtureSpec)
    permissions: EvalPermissions = Field(default_factory=EvalPermissions)
    budget: EvalBudget = Field(default_factory=EvalBudget)
    history: list[dict[str, Any]] = Field(default_factory=list)
    mocks: MockSpec = Field(default_factory=MockSpec)
    script: list[ScriptRound] = Field(default_factory=list)
    expect: EvalExpectation = Field(default_factory=EvalExpectation)

    @property
    def supports_real(self) -> bool:
        return "real" in self.tags


class TraceCall(StrictModel):
    name: str
    arguments: dict[str, Any]
    output: str = ""
    is_error: bool = False
    denied: bool = False


class CheckResult(StrictModel):
    code: str
    passed: bool
    message: str


class CaseMetrics(StrictModel):
    tool_calls: int = 0
    illegal_tool_calls: int = 0
    repeated_tool_calls: int = 0
    permission_denials: int = 0
    confirmations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


class CaseResult(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    case_id: str
    mode: Literal["scripted", "real"]
    repetition: int = 1
    passed: bool
    outcome: Literal["success", "error", "interrupted"]
    final: str = ""
    calls: list[TraceCall] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    metrics: CaseMetrics = Field(default_factory=CaseMetrics)
    error: str = ""
    prompt_hash: str = ""
    tool_schema_hash: str = ""


class RunMetadata(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    mode: Literal["scripted", "real"]
    model_capability: bool
    git_commit: str
    python: str
    platform: str
    provider: str
    model: str
    prompt_hash: str
    tool_schema_hash: str
    permission_mode: str
    skills_enabled: bool
    mcp_enabled: bool
    compaction_enabled: bool
    started_at: str


class RunSummary(StrictModel):
    type: Literal["summary"] = "summary"
    metadata: RunMetadata
    cases: int
    passed: int
    success_rate: float
    tool_calls: int
    illegal_tool_rate: float
    repeat_rate: float
    input_tokens: int
    output_tokens: int
    duration_ms: int
