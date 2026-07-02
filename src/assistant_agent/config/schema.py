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


class ToolsConfig(BaseModel):
    """工具行为与安全设置。"""

    confirm_dangerous_shell: bool = Field(default=True, description="危险 shell 操作前是否要求确认")
    shell_timeout: int = Field(default=60, gt=0, description="shell 命令超时（秒）")


class UIConfig(BaseModel):
    """终端 UI 行为。"""

    show_reasoning: bool = Field(
        default=False, description="是否实时显示模型的思考（reasoning）过程；关闭时只显示 spinner"
    )


class AppConfig(BaseModel):
    """顶层配置。"""

    active: str = Field(..., description="当前启用的 provider，对应 providers 的某个 key")
    providers: dict[str, ProviderConfig] = Field(..., min_length=1)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

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
