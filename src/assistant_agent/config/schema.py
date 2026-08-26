"""配置数据模型（Pydantic）。

核心约束：模型调用所需的一切（provider、model、key、endpoint、参数）都来自配置，
业务代码不写死任何 provider。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
    context_window: int | None = Field(
        default=None,
        gt=0,
        description="模型实际上下文窗口；声明后会拒绝超过该能力的 agent 配置",
    )
    request_timeout: float = Field(
        default=120, gt=0, le=3600, description="单次模型请求最大等待秒数"
    )
    image_input: Literal["auto", "enabled", "disabled"] = Field(
        default="auto", description="图片输入能力：显式启停，或使用可靠模型元数据自动判断"
    )
    unknown_image_token_reserve: int = Field(
        default=2048, ge=256, le=32768, description="图片 token 无可靠算法时的内部安全预留"
    )


class AttachmentsConfig(BaseModel):
    """用户输入附件的解析、存储与上下文硬限制。"""

    max_attachments_per_message: int = Field(default=8, ge=1, le=32)
    max_images_per_message: int = Field(default=4, ge=1, le=16)
    max_total_bytes_per_run: int = Field(default=20 << 20, ge=1024)
    max_text_bytes: int = Field(default=512 << 10, ge=1024)
    max_text_chars: int = Field(default=300_000, ge=1000)
    max_image_bytes: int = Field(default=8 << 20, ge=1024)
    max_image_pixels: int = Field(default=20_000_000, ge=1)
    max_image_edge: int = Field(default=8192, ge=1, le=32768)
    max_context_ratio: float = Field(default=0.30, gt=0, le=0.5)
    max_context_tokens: int = Field(default=8192, ge=128)
    unbound_ttl_seconds: int = Field(default=3600, ge=60, le=604800)


class OutputConfig(BaseModel):
    """用户交付文件的受管存储设置。"""

    root: str = Field(default="outputs", min_length=1)
    layout: Literal["flat", "date", "date_session"] = "date_session"
    max_file_bytes: int = Field(default=10 << 20, ge=1024)
    max_run_files: int = Field(default=20, ge=1, le=200)
    max_run_bytes: int = Field(default=50 << 20, ge=1024)
    max_session_files: int = Field(default=100, ge=1, le=2000)
    max_session_bytes: int = Field(default=200 << 20, ge=1024)
    max_chunk_bytes: int = Field(default=8192, ge=1024, le=65536)
    max_draft_chunks: int = Field(default=256, ge=1, le=4096)
    allowed_media_types: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {"text/html", "text/markdown", "text/csv", "application/json", "text/plain"}
        )
    )
    preview_media_types: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {"text/html", "text/markdown", "text/csv", "application/json", "text/plain"}
        )
    )

    @model_validator(mode="after")
    def _limits_are_ordered(self) -> OutputConfig:
        if self.max_run_bytes < self.max_file_bytes:
            raise ValueError("outputs.max_run_bytes 不能小于 max_file_bytes")
        if self.max_session_bytes < self.max_run_bytes:
            raise ValueError("outputs.max_session_bytes 不能小于 max_run_bytes")
        if not self.preview_media_types <= self.allowed_media_types:
            raise ValueError("outputs.preview_media_types 必须是 allowed_media_types 子集")
        return self


class CompactionConfig(BaseModel):
    """上下文摘要压缩（M8b）。默认关闭——关闭时上下文行为逐字节等于硬截断现状。"""

    enabled: bool = Field(
        default=False, description="开启后历史逼近预算时把最旧对话压成摘要（替代硬丢），防失忆"
    )
    threshold: float = Field(
        default=0.8, gt=0, le=1, description="未截断历史占预算比例超过此值时触发压缩"
    )
    keep_recent_turns: int = Field(
        default=4, gt=0, description="绝不压缩的最近完整用户轮数（保护窗）"
    )
    summary_model: str = Field(
        default="", description="生成摘要用的 provider 名；空=用当前对话模型。不在业务里硬写模型"
    )
    summary_max_tokens: int = Field(
        default=512, gt=0, description="摘要写入上下文前的保守 token 硬上限"
    )


class RecoveryConfig(BaseModel):
    """步骤级 checkpoint 与单机恢复设置。"""

    enabled: bool = Field(default=True, description="保存 Run checkpoint，支持崩溃后恢复")
    dir: str = Field(
        default=".assistant_agent/runs",
        description="Run checkpoint 目录；默认兼容值解析到用户级 workspace state",
    )
    max_completed_runs: int = Field(
        default=100, ge=0, description="最多保留的已同步 terminal Run 数量"
    )


class ContinuationConfig(BaseModel):
    """只扩展当前 Run 的预算 continuation 安全边界。"""

    max_extensions: int = Field(default=2, ge=0, le=20)
    iteration_increment: int = Field(default=10, gt=0)
    max_iterations_hard: int = Field(default=100, gt=0)
    tool_call_increment: int = Field(default=20, gt=0)
    max_tool_calls_hard: int = Field(default=200, gt=0)
    tool_output_increment: int = Field(default=40_000, gt=0)
    max_tool_output_chars_hard: int = Field(default=400_000, gt=0)


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
    tool_result_context_chars: int = Field(
        default=1600,
        ge=0,
        description="上下文压力较高时，较早工具结果在模型视图中的字符上限；0=不压缩",
    )
    recent_tool_result_blocks: int = Field(
        default=3,
        ge=0,
        description="始终保留原文的最近工具批次数",
    )
    max_total_tool_output_chars: int = Field(
        default=50_000,
        ge=0,
        description="单任务写入上下文的工具结果累计字符上限；0=不限制",
    )
    reserved_output_tokens: int = Field(
        default=1024,
        ge=0,
        description="从上下文预算中预留给模型回复的 token；0=不预留（回退旧口径）",
    )
    compaction: CompactionConfig = Field(default_factory=lambda: CompactionConfig())
    recovery: RecoveryConfig = Field(default_factory=lambda: RecoveryConfig())
    continuation: ContinuationConfig = Field(default_factory=ContinuationConfig)

    @model_validator(mode="after")
    def _continuation_limits_cover_initial_budget(self) -> AgentConfig:
        limits = self.continuation
        if limits.max_iterations_hard < self.max_iterations:
            raise ValueError("continuation.max_iterations_hard 不能小于 max_iterations")
        if limits.max_tool_calls_hard < self.max_tool_calls:
            raise ValueError("continuation.max_tool_calls_hard 不能小于 max_tool_calls")
        if (
            self.max_total_tool_output_chars > 0
            and limits.max_tool_output_chars_hard < self.max_total_tool_output_chars
        ):
            raise ValueError(
                "continuation.max_tool_output_chars_hard 不能小于 max_total_tool_output_chars"
            )
        return self


class ToolsConfig(BaseModel):
    """工具行为与安全设置。"""

    confirm_dangerous_shell: bool = Field(
        default=True,
        description="已废弃兼容字段；统一权限边界始终启用，不能用此字段关闭",
    )
    shell_timeout: int = Field(default=60, gt=0, description="shell 命令超时（秒）")
    max_background_processes: int = Field(
        default=4, ge=1, le=32, description="单个 Runtime 最多同时运行的受管后台进程数"
    )
    max_background_output_chars: int = Field(
        default=100_000,
        gt=0,
        description="每个后台进程 stdout/stderr 分别保留的最大字符数",
    )
    max_output_chars: int = Field(
        default=4000,
        ge=0,
        description="单个工具结果返回 UI/上下文的最大字符数；0=不截断",
    )
    max_captured_output_chars: int = Field(
        default=1_000_000,
        gt=0,
        description="Shell/Git 每个 stdout/stderr 流最多在内存/artifact 保留的字符数",
    )
    max_artifact_files: int = Field(
        default=100,
        gt=0,
        description="workspace .assistant_agent/artifacts 内最多保留的工具输出文件数",
    )


class SandboxConfig(BaseModel):
    """内置工具执行环境。workspace 是应用层约束，container 才是 OS 隔离。"""

    mode: Literal["off", "workspace", "container"] = "off"
    filesystem: Literal["host", "workspace", "read_only"] | None = None
    process: Literal["host", "confined", "container"] | None = None
    extensions: Literal["host", "disabled", "container"] | None = None
    engine: Literal["docker", "podman"] = "docker"
    image: str = "python:3.11-slim"
    network: Literal["none", "bridge"] = "none"
    memory: str = "1g"
    cpus: float = Field(default=1.0, gt=0, le=64)
    pids_limit: int = Field(default=256, ge=16, le=4096)
    user: str = Field(
        default="auto",
        min_length=1,
        description="容器内非 root 用户；auto 在 POSIX 映射当前 uid/gid，其他平台用 65534",
    )

    @model_validator(mode="after")
    def _bound_profile_to_mode(self) -> SandboxConfig:
        expected = {
            "off": ("host", "host"),
            "workspace": ("workspace", "confined"),
            "container": ("workspace", "container"),
        }[self.mode]
        if self.filesystem is None:
            object.__setattr__(self, "filesystem", expected[0])
        if self.process is None:
            object.__setattr__(self, "process", expected[1])
        if self.extensions is None:
            object.__setattr__(
                self,
                "extensions",
                "container" if self.mode == "container" else "disabled",
            )
        if (self.filesystem, self.process) != expected:
            raise ValueError("sandbox.filesystem/process 必须与 sandbox.mode 的执行边界一致")
        if self.mode == "container" and self.extensions == "host":
            raise ValueError("sandbox.container 不允许 extensions=host")
        return self

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_off_is_sandbox_off(cls, value: object) -> object:
        """PyYAML 按 YAML 1.1 把裸 `off` 解析为 False。"""
        return "off" if value is False else value

    @field_validator("user")
    @classmethod
    def _reject_root_user(cls, value: str) -> str:
        normalized = value.strip()
        user_part = normalized.split(":", 1)[0].lower()
        if user_part == "root" or (user_part.isdecimal() and int(user_part) == 0):
            raise ValueError("sandbox.user 不能使用 root 用户")
        return normalized


class PermissionRuleConfig(BaseModel):
    effect: Literal["allow", "ask", "deny"]
    capability: Literal[
        "filesystem.read",
        "filesystem.write",
        "process.execute",
        "network.access",
        "mcp.call",
        "skill.load",
        "user.interaction",
        "extension.manage",
    ]
    target: str = "*"
    tool: str = "*"


class PermissionsConfig(BaseModel):
    """M9b 应用层权限策略；不等同于 OS 沙箱。"""

    mode: Literal["readonly", "workspace", "strict", "unrestricted"] = "workspace"
    rules: list[PermissionRuleConfig] = Field(default_factory=list)
    sensitive_paths: list[str] = Field(default_factory=list)


class UIConfig(BaseModel):
    """终端 UI 行为。"""

    display_mode: Literal["normal", "verbose", "quiet"] = Field(
        default="normal", description="CLI 展示密度：normal / verbose / quiet"
    )
    show_reasoning: bool = Field(
        default=False, description="是否实时显示模型的思考（reasoning）过程；关闭时只显示 spinner"
    )


class LoggingConfig(BaseModel):
    """结构化事件日志与工具审计。日志只落本地，随 .assistant_agent/ gitignore 不入库。"""

    enabled: bool = Field(default=True, description="是否记录结构化事件日志")
    dir: str = Field(
        default=".assistant_agent/logs",
        description="JSONL 日志目录；默认兼容值解析到用户级 workspace state",
    )
    log_tool_io: bool = Field(
        default=True,
        description="是否记录工具参数/输出载荷（截断+脱敏）；关掉则只记元数据（名/耗时/状态/长度）",
    )
    max_payload_chars: int = Field(
        default=2000, gt=0, description="单个参数/输出载荷记录的最大字符数，超出截断"
    )


class WebSearchConfig(BaseModel):
    """Web 搜索 backend 设置。"""

    backend: Literal["duckduckgo", "searxng"] = Field(
        default="duckduckgo", description="搜索 backend；duckduckgo 无需 key"
    )
    max_results: int = Field(default=10, ge=1, le=20, description="单次搜索结果硬上限")
    retry_attempts: int = Field(default=1, ge=0, le=2, description="可重试搜索错误的额外尝试次数")
    searxng_url: str = Field(default="", description="SearXNG 实例 base URL")

    @model_validator(mode="after")
    def _searxng_requires_url(self) -> WebSearchConfig:
        if self.backend == "searxng" and not self.searxng_url.startswith(("http://", "https://")):
            raise ValueError("search.backend=searxng 时必须配置有效 searxng_url")
        return self


class WebConfig(BaseModel):
    """结构化联网工具设置。"""

    enabled: bool = Field(default=True, description="是否注册 web_search/fetch_url")
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    request_timeout: float = Field(default=15, gt=0, le=120, description="单个 HTTP 请求超时（秒）")
    max_response_bytes: int = Field(
        default=2_000_000, ge=1024, le=20_000_000, description="抓取响应体解压后的字节上限"
    )
    max_content_chars: int = Field(
        default=30_000, ge=1000, le=200_000, description="提取正文字符上限"
    )
    max_redirects: int = Field(default=5, ge=0, le=10, description="重定向硬上限")


class SkillsConfig(BaseModel):
    """技能（SKILL.md 指示书）发现设置。"""

    enabled: bool = Field(default=True, description="是否发现并注入技能")
    dirs: list[str] = Field(
        default_factory=list,
        description="技能目录；留空用默认（项目 skills/ + 用户安装目录）",
    )
    trusted_project_skills: list[str] = Field(
        default_factory=list,
        description="显式信任的项目/自定义 Skill 名称；未列出的内容进入提示词前需确认",
    )
    catalog_max_chars: int = Field(
        default=8000,
        ge=256,
        le=50_000,
        description="初始提示词中 Skill 元数据目录的字符硬上限",
    )


class MCPToolPolicyConfig(BaseModel):
    """客户端明确赋予单个 MCP 工具的执行语义，不参与权限放行。"""

    replay: Literal["default", "safe_readonly", "requires_decision"] = Field(
        default="default", description="进程恢复时是否可自动重放"
    )
    outcome_on_transport_error: Literal["default", "unknown"] = Field(
        default="default", description="发送后传输失败是否视为结果未知"
    )
    timeout: int | None = Field(default=None, ge=1, description="覆盖 server 的单次调用超时")


class MCPServerConfig(BaseModel):
    """单个 MCP server 连接设置（stdio 本地 / http 远程）。"""

    type: str = Field(default="stdio", description="传输方式：stdio（本地子进程）或 http（远程）")
    # stdio 用
    command: str = Field(default="", description="[stdio] 启动 server 的命令，如 npx")
    args: list[str] = Field(default_factory=list, description="[stdio] 命令参数")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="[stdio] 环境变量；值支持 ${VAR} 从进程环境插值，密钥不落配置",
    )
    cwd: str = Field(
        default="",
        description="[stdio] server 工作目录；空=按 server 隔离的受管运行目录",
    )
    # http 用
    url: str = Field(default="", description="[http] server 的 endpoint URL")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="[http] 请求头；值支持 ${VAR} 从进程环境插值（如 Bearer ${TOKEN}）",
    )
    # 通用
    enabled: bool = Field(default=True, description="是否启用该 server")
    startup: Literal["optional", "required"] = Field(
        default="optional", description="启动失败时降级继续或使当前 Runtime 创建失败"
    )
    connect_timeout: int | None = Field(
        default=None,
        ge=1,
        description="连接、initialize 和 tools/list 超时；空值兼容使用 timeout",
    )
    timeout: int = Field(default=30, ge=1, description="单次工具调用超时（秒）")
    auto_approve: bool = Field(
        default=False, description="为 true 时该 server 工具跳过危险确认（默认都需确认）"
    )
    trust_tool_annotations: bool = Field(
        default=False,
        description="是否信任该 server 的只读 annotation；高风险 annotation 始终生效",
    )
    tool_policies: dict[str, MCPToolPolicyConfig] = Field(
        default_factory=dict,
        description="按原始 MCP 工具名覆盖恢复、失败结果和超时语义",
    )
    max_tools: int = Field(default=40, ge=1, description="该 server 注册的工具数上限")
    include_tools: list[str] = Field(
        default_factory=list, description="工具白名单；空=全部（再受上限约束）"
    )
    exclude_tools: list[str] = Field(default_factory=list, description="工具黑名单")

    @model_validator(mode="after")
    def _check_transport(self) -> MCPServerConfig:
        if self.type == "stdio":
            if not self.command:
                raise ValueError("stdio server 必须配 command")
        elif self.type == "http":
            if not self.url:
                raise ValueError("http server 必须配 url")
        else:
            raise ValueError(f"未知 MCP transport type：{self.type}（支持 stdio/http）")
        return self


class MCPConfig(BaseModel):
    """MCP client 设置。"""

    enabled: bool = Field(default=True, description="是否连接 MCP server 并注册其工具")
    max_total_tools: int = Field(
        default=60, ge=1, description="全局 MCP 工具数上限，防 schema 撑爆上下文"
    )
    connect_parallelism: int = Field(
        default=4, ge=1, le=32, description="MCP server 启动连接的最大并发数"
    )
    servers: dict[str, MCPServerConfig] = Field(
        default_factory=dict, description="server 名 → 连接设置"
    )


class AppConfig(BaseModel):
    """顶层配置。"""

    active: str = Field(..., description="当前启用的 provider，对应 providers 的某个 key")
    providers: dict[str, ProviderConfig] = Field(..., min_length=1)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)
    outputs: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _active_must_exist(self) -> AppConfig:
        if self.active not in self.providers:
            available = ", ".join(sorted(self.providers))
            raise ValueError(f"active='{self.active}' 不在 providers 中。可选：{available}")
        summary_model = self.agent.compaction.summary_model
        if summary_model and summary_model not in self.providers:
            available = ", ".join(sorted(self.providers))
            raise ValueError(
                f"summary_model='{summary_model}' 不在 providers 中。可选：{available}"
            )
        context_window = self.active_provider.context_window
        if context_window is not None and self.agent.max_context_tokens > context_window:
            raise ValueError("agent.max_context_tokens 超过 active provider 声明的 context_window")
        if self.agent.reserved_output_tokens > self.active_provider.max_tokens:
            raise ValueError("agent.reserved_output_tokens 不能超过 active provider 的 max_tokens")
        return self

    @property
    def active_provider(self) -> ProviderConfig:
        """当前启用的 provider 配置。"""
        return self.providers[self.active]
