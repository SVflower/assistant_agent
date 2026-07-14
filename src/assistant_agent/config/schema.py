"""配置数据模型（Pydantic）。

核心约束：模型调用所需的一切（provider、model、key、endpoint、参数）都来自配置，
业务代码不写死任何 provider。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ProviderConfig(BaseModel):
    """单个模型后端的配置。云端和本地共用此结构，区别仅在字段取值。"""

    model: str = Field(
        ..., description="LiteLLM 模型名，如 anthropic/claude-sonnet-4-6 或 openai/local-model"
    )
    api_key: str | None = Field(default=None, description="API key；为空时回退到环境变量")
    api_base: str | None = Field(
        default=None, description="本地/自托管端点的 base_url，如 http://localhost:1234/v1"
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)


class AgentConfig(BaseModel):
    """Agent 循环行为。"""

    max_iterations: int = Field(default=25, gt=0, description="单任务最大工具调用轮数，防跑飞")
    max_history_messages: int = Field(
        default=40, gt=0, description="上下文保留的最大消息数（硬上限兜底）"
    )
    max_context_tokens: int = Field(
        default=8000,
        gt=0,
        description="上下文 token 预算；超出则丢弃最旧消息。本地小模型窗口小，务必按需下调",
    )
    max_tool_calls: int = Field(
        default=50,
        gt=0,
        description="单任务最多允许执行的工具调用数，限制单轮批量调用和跨轮累计",
    )
    max_total_tool_output_chars: int = Field(
        default=50_000,
        ge=0,
        description="单任务写入上下文的工具结果累计字符上限；0=不限制",
    )


class ToolsConfig(BaseModel):
    """工具行为与安全设置。"""

    confirm_dangerous_shell: bool = Field(default=True, description="危险 shell 操作前是否要求确认")
    shell_timeout: int = Field(default=60, gt=0, description="shell 命令超时（秒）")
    max_output_chars: int = Field(
        default=4000,
        ge=0,
        description="单个工具结果返回 UI/上下文的最大字符数；0=不截断",
    )


class UIConfig(BaseModel):
    """终端 UI 行为。"""

    show_reasoning: bool = Field(
        default=False, description="是否实时显示模型的思考（reasoning）过程；关闭时只显示 spinner"
    )


class LoggingConfig(BaseModel):
    """结构化事件日志与工具审计。日志只落本地，随 .assistant_agent/ gitignore 不入库。"""

    enabled: bool = Field(default=True, description="是否记录结构化事件日志")
    dir: str = Field(default=".assistant_agent/logs", description="JSONL 日志目录（按天分卷）")
    log_tool_io: bool = Field(
        default=True,
        description="是否记录工具参数/输出载荷（截断+脱敏）；关掉则只记元数据（名/耗时/状态/长度）",
    )
    max_payload_chars: int = Field(
        default=2000, gt=0, description="单个参数/输出载荷记录的最大字符数，超出截断"
    )


class AppConfig(BaseModel):
    """顶层配置。"""

    active: str = Field(..., description="当前启用的 provider，对应 providers 的某个 key")
    providers: dict[str, ProviderConfig] = Field(..., min_length=1)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @model_validator(mode="after")
    def _active_must_exist(self) -> AppConfig:
        if self.active not in self.providers:
            available = ", ".join(sorted(self.providers))
            raise ValueError(f"active='{self.active}' 不在 providers 中。可选：{available}")
        return self

    @property
    def active_provider(self) -> ProviderConfig:
        """当前启用的 provider 配置。"""
        return self.providers[self.active]
