# Assistant Agent 服务调用指南

> 适用对象：需要在 Python 进程内调用 `assistant_agent` 的 Web API、桌面应用、后台任务、自动化平台
> 或其他上层服务。
>
> 本文是公共服务契约的长期唯一正式入口；里程碑归档和阶段性交接不能替代本文。
> 当前公共事件契约：`EVENT_CONTRACT_VERSION == 1`；Session 服务契约：
> `SESSION_CONTRACT_VERSION == 5`；当前 Run checkpoint：schema v10；当前 Session 文档：schema v5；
> 当前 Output contract：v1。
> 最近同步：M33 Native ArtifactWriter（2026-08-14）。
>
> **破坏性契约版本：`AGENT_SERVICE_CONTRACT_VERSION = 5`。** Agent 只读取和写入 RunState v10、
> Session v5、Chart V2、Attachment/Content v1 和 Output v1；旧状态不迁移。

## M33 Managed Output

模型可见的 `create_output` 只接受 filename/media_type/title/disposition，不接受 content。工具登记意图后，
Runtime 在下一模型轮禁用全部工具，把普通文本流写入私有草稿并自动分块、校验和 finalize；模型、API、Web
均不知道 draft_id/chunk_index/finalize。默认分块上限为 8192 UTF-8 bytes，并受既有总量限制。
草稿仅属于 Agent 内部存储，不进入公共 DTO 或网络事件。成功的 `tool_result` 通过
`StepEvent.output: OutputArtifactV1 | None` 发布小型引用，Event contract 保持 v1；内容不进入事件。
Output ref 包含 opaque `output_id`、Session/Run/message/call 归属、filename/title、MIME、大小、SHA-256、
时间、disposition 和 preview_supported，绝不包含服务器路径。

调用方只使用 `SessionRuntime.list_outputs()`、`get_output()`、`get_output_payload()`，或 AgentService
同名 Session-scoped 接口。API 不扫描 `outputs/`、不读取 sidecar、不复制幂等或删除状态机。Session
删除级联 Output；fork 深复制边界前关联文件；Run 失败不删除已发布文件。HTML 仅作为数据，Web 预览
必须严格 sandbox。稳定错误类型为 OutputInvalid/LimitExceeded/NotFound/Conflict/Unavailable。

捕获正文不发布 `content_delta`。暂停/崩溃恢复时丢弃半文件并从头捕获；取消或失败不发布 artifact。
成功仍以原 `create_output` call_id 的 `tool_result` 发布唯一 OutputArtifactV1。API 不管理草稿。
当前契约为 service v5、Session v5、RunState v10、Output v1、Event v1。
> Session v5、ChartSpec/ChartArtifact V2、Attachment/Content v1 与 Output v1；不再读取或迁移旧版本。调用方
> 升级前必须清理旧测试状态，并删除所有 V1 图表与旧迁移分支。

## M32 Attachment 输入

调用方从 `assistant_agent.service` 导入 `AttachmentUploadV1`、`UserMessageInputV1`、
`MessageContentV1`、`TextPartV1`、`AttachmentPartV1`。先对目标 `SessionRuntime` 调用
`ingest_attachments(uploads)`，再把返回的 `AttachmentSummaryV1.attachment` 放入用户 content parts 并
调用 `start_run(input)`。Attachment ref 只有 opaque ID、安全元数据与 hash，绝不包含服务器 path、URL、
base64、EXIF 或正文。

```python
summaries = runtime.ingest_attachments([AttachmentUploadV1(data, "report.csv", "text/csv")])
content = MessageContentV1(parts=(
    TextPartV1(text="分析这个文件"),
    AttachmentPartV1(attachment=summaries[0].attachment),
))
execution = runtime.start_run(UserMessageInputV1(content=content))
```

文本附件白名单为 txt/md/csv/json/log/xml/yaml，图片白名单为 PNG/JPEG/WebP。图片能力由服务端
`RuntimePolicy` 与 `ProviderConfig.image_input` 决定；unknown 视为不支持。超限错误稳定为
`attachment_too_large`、`attachment_context_too_large`、`unsupported_input_modality`。Session 删除级联
附件；Run 删除不删除已写入 Session 的附件；fork 深复制附件并产生新 ID。checkpoint 仅保存 ref，调用
provider 时临时物化并释放。

## 1. 集成边界

`assistant_agent` 提供同步、UI 无关的 Python 服务接口。调用方不需要启动 CLI 子进程，也不应解析
Rich 终端输出。

```text
上层服务
  -> assistant_agent.service       Runtime、Session、Run 与恢复用例
  -> assistant_agent.contracts     StepEvent、失败、能力与 Interaction DTO/Protocol
  -> assistant_agent.interaction   安全默认和同步阻塞 Interaction 实现
  -> assistant_agent 内部实现      LLM、Tools、Skills、MCP、Workspace、checkpoint
```

上层服务负责：

- HTTP、WebSocket、桌面 UI 或消息队列协议；
- 用户认证、Origin/CORS、限流和租户边界；
- 工作线程、事件序号、时间戳、心跳、缓存和断线重连；
- 把公共事件转换成自己的展示 DTO；
- 默认过滤敏感 reasoning。

Agent 负责：

- Runtime 装配及资源回滚；
- Session/Run 持久化和 checkpoint；
- 同一 Session 的单 Run 约束；
- 暂停、取消、恢复和不确定副作用处理；
- 权限策略、授权记忆和审计；
- MCP、WebClient、Workspace 和受管进程的生命周期。

## 2. 安装与版本固定

开发期可安装本地仓库：

```powershell
pip install -e D:\Dev\AI\assistant_agent
```

生产或跨项目协作时，应构建 wheel 或固定包含所需公共契约的 Git commit。不要依赖一个持续移动的
分支，也不要从源码目录拼接 `PYTHONPATH`。

调用方启动时必须检查事件契约版本：

```python
from assistant_agent.service import EVENT_CONTRACT_VERSION

EXPECTED_EVENT_CONTRACT_VERSION = 1
if EVENT_CONTRACT_VERSION != EXPECTED_EVENT_CONTRACT_VERSION:
    raise RuntimeError(
        f"不支持的 Agent 事件契约：{EVENT_CONTRACT_VERSION}，"
        f"期望 {EXPECTED_EVENT_CONTRACT_VERSION}"
    )
```

只从以下公共模块导入：

```python
from assistant_agent.contracts import ...
from assistant_agent.interaction import ...
from assistant_agent.service import ...
```

从 `assistant_agent.service` 或 `assistant_agent.interaction` 根入口导入的公共类型保持
同一 Python 类型身份。DTO/Protocol 优先从 `contracts` 获取，从 `interaction` 获取
`SafeDefaultInteractionPort` / `BlockingInteractionPort` 实现，从 `service` 获取生命周期门面。

不要导入：

```python
assistant_agent.cli
assistant_agent.ui
assistant_agent.agent
assistant_agent.application
assistant_agent.bootstrap
assistant_agent.config
assistant_agent.execution
assistant_agent.integrations
assistant_agent.observability
assistant_agent.persistence
assistant_agent.providers
assistant_agent.tools
```

这些模块不是服务集成协议，直接依赖会绕过公共生命周期或造成升级耦合。

### 2.1 M19 导入策略

- `EVENT_CONTRACT_VERSION` 保持 `1`，StepEvent 字段、默认值和事件顺序未破坏；
- Run checkpoint 保持 schema v3，旧 checkpoint 无需迁移；
- `AgentService`、`AgentRuntime`、`SessionRuntime`、`create_runtime` 的公共根导入和构造签名不变；
- `RuntimeNotice` 等稳定 DTO 可统一从 `assistant_agent.contracts` 根入口导入；
- `RunExecution` 保留可选 `warning: str = ""`，用于 checkpoint 回退等诊断；调用方须
  自行脱敏，不能直接透传网络；
- `llm/mcp/obs/runtime/session/skills/web` 等迁移前顶层包，以及 Agent/Tool/Service 的旧转发模块已删除；
- 这是开发期内部 Python import 的破坏性清理，不提升 `EVENT_CONTRACT_VERSION`，因为 StepEvent/DTO
  和运行语义没有变化；
- API/Web 若只依赖三个公共根入口，无运行逻辑修改；若曾穿透内部目录，必须迁回公共入口并增加
  禁止内部 import 的架构测试。

### 2.2 M20 启动与扩展契约

- `EVENT_CONTRACT_VERSION` 保持 `1`；StepEvent、Run checkpoint、Interaction 和终态顺序无变化；
- `RuntimeStartupEvent` 从 `assistant_agent.contracts` 导出，不从 `assistant_agent.service` 转发；
- `create_runtime(..., startup_observer=...)` 可接收同步 observer，用于低层宿主展示 Runtime 创建进度；
- observer 只收到安全阶段事实，不属于 Run 事件流；observer 异常会被忽略，不能阻断创建；
- `RuntimeCapabilities.mcp_servers[].status` 向后兼容增加状态，调用方必须把未知状态按 unavailable 处理；
- `SessionRuntime.capabilities` 每次访问都会刷新 MCP 运行状态，不能把首次快照永久缓存；
- optional MCP 的工具目录只表示 Schema 可用，不表示 server 已连接；工具列表在当前 Runtime 内保持稳定。

### 2.3 M21 命令与后台进程契约

- `EVENT_CONTRACT_VERSION` 保持 `1`，Run checkpoint 保持 v3，终态和恢复顺序不变；
- `ToolDisplay` 向后兼容增加可选 `timeout_seconds`；前台 Shell 调用提供安全 deadline，调用方不得从
  完整命令推断 timeout；
- 上下文预算允许时，`RuntimeCapabilities.tools` 增加 `manage_process`。工具清单是动态能力，API 不得
  写死或因某个 Runtime 省略该工具而启动失败；
- `manage_process` 的 action 为 `start/status/logs/stop/list`，进程引用是 Runtime 内 opaque
  `proc-<12 hex>`；它不是 OS PID，不跨 Runtime、重启或 SessionRuntime 淘汰保持有效；
- 后台输出有界，`tool_result.result_metadata` 仅提供 process_id、status、returncode、elapsed_seconds
  和 stdout/stderr bytes，不提供完整命令、环境变量、OS PID 或原始异常；
- Runtime close 会终止它拥有的所有后台进程。API 不得另建后台进程表，也不得在 Runtime close 后
  自动重启旧 process ID；
- `run_shell` 检测到继承管道的后台后代时返回 `result_code=background_process_detected`，不产生
  Run terminal failure；模型可改用 `manage_process`。若进程在 tool started/completed checkpoint 边界
  崩溃，仍沿用既有 `tool_uncertain`，不得自动重放 start；
- container Workspace 当前返回 `managed_process_container_unsupported`，绝不退化到宿主执行。

### 2.4 M24 图表与 Presentation Artifact 契约

- `EVENT_CONTRACT_VERSION` 保持 `1`，不新增 EventKind；`ToolResult/StepEvent` additive 增加
  `chart: ChartArtifact | None`。
- Run checkpoint 升 v4；读取 v1/v2/v3 时迁移为 `presentations=[]`。旧 Agent 不承诺降级读取 v4。
- Agent 是 Artifact 唯一权威所有者。API 不保存第二份完整数据，不解析 checkpoint/Session 文件。
- `present_chart` 是纯、安全幂等工具。模型只能提交受控 `ChartSpecV1`，不能提交 ECharts option、
  formatter、HTML、URL、graphic、脚本、函数或颜色配置。
- 工具 schema 占用超过上下文预算时 Runtime 可省略 `present_chart`，并通过
  `chart_presentation_omitted_context_limit` notice 报告；调用方以 `RuntimeCapabilities.tools` 为准。
- 成功事件顺序固定为 `tool_call -> tool_result(chart) -> final -> run_terminal`。Artifact 已原子
  checkpoint 后才发 `tool_result`；失败产生 `artifact_rejected` notice，不改变正文和终态。

### 2.5 M25 Web Runtime 与 Interaction 截止时间

- 浏览器访问服务器 Agent 必须显式使用 `RuntimePolicy.web()`，不得使用 CLI 默认策略；profile 由可信
  服务配置选择，不进入模型参数。
- `RuntimeCapabilities.profile` additive 返回 `cli/service/web/custom`。Web capabilities 是最终实际注册
  工具事实；API 仍应按部署 allowlist 二次过滤。
- Web profile 注册 `web_search`、受信 `load_skill`、`inspect_runtime`、`ask_user` 和 `present_chart`
  （后两项仍受实际配置/上下文预算影响）；不注册服务器文件、Git、Shell、进程、扩展管理、任意 MCP
  或 `fetch_url`。
- Web profile 会发现服务器管理员预装在 `~/.assistant_agent/skills` 的 personal Skill，并通过
  capabilities 和 `load_skill` 只读使用。浏览器请求和模型不能安装、删除或修改 Skill；Skill 指令也
  不能扩大 Web 工具白名单，因此不会获得服务器文件、Shell 或进程能力。
- `fetch_url` 暂缓是因为现有 URL 校验尚未把 DNS 结果绑定实际连接，不能证明抵御 DNS rebinding。
- Web allowlist 工具的权限请求由部署策略自动允许，不产生 approval；config 显式 deny 仍可收紧。
- `InteractionRequestBase.expires_at` 是 UTC RFC 3339 字符串，由 `BlockingInteractionPort` 入队时按实际
  timeout 生成。调用方必须转发，不重新计算。
- question 新增 `legal_options=(answer, unavailable)`；回答候选仍在 `options`。其他四类继续使用既有
  `legal_options`。
- `SessionRuntime.pause()/cancel()` 会中断待处理 Blocking Interaction；中断、timeout、close、异常和
  晚到响应均 fail closed。port 保持可用于 pause 后的恢复。
- Event contract 仍为 v1，checkpoint 仍为 v4；Interaction 是调用方网络事件，不是 StepEvent。

### 2.6 M25-AGENT-02 Run 终态所有权

- `SessionRuntime.cancel_run(run_id) -> RunExecution` 是 additive 公共能力，用于 worker 已退出后的 paused
  Run；首次取消原子保存 cancelled/terminal、同步 Session，并返回一个真实
  `run_terminal(cancelled)`。
- 已 cancelled 的重复取消幂等并返回空事件流；completed/failed、错误 Session 归属和仍由 worker 管理
  的 running Run 不允许改写。active Run 继续使用 `cancel()` 并由原 worker 消费 terminal。
- 公共事件 Iterator 内的未分类普通异常由 Agent 保存为脱敏 `internal_error` failed terminal，再同步
  Session 并发布唯一 `run_terminal`。调用方不得合成 terminal、解析 checkpoint 或复制 Session 同步状态机。
- 消费者主动关闭 Iterator 仍保存 paused；`GeneratorExit`、`KeyboardInterrupt` 和 `SystemExit` 不会被误记
  为内部 failed。
- Event contract 保持 v1，checkpoint 保持 v4；方法与异常所有权属于向后兼容扩展。

## 3. 推荐入口

业务服务优先使用 `AgentService`。它收编了 Session、Run、恢复和终态同步，不要求调用方复制状态机。

```python
from pathlib import Path

from assistant_agent.service import AgentService, RuntimePolicy

policy = RuntimePolicy(
    allow_extension_management=False,
    allow_personal_skills=True,
    allowed_mcp_transports=frozenset({"http"}),
    minimum_sandbox="workspace",
)

service = AgentService(
    config_path=Path(r"D:\server-config\assistant-agent.yaml"),
    workspace_root=Path(r"D:\server-workspaces\project-a"),
    runtime_policy=policy,
)
```

两个路径必须由服务端配置决定，不能来自单次用户消息。`workspace_root` 同时决定文件操作边界和默认
状态命名空间；Runtime 不修改全局 `os.chdir()`。

低层 `create_runtime()` 主要供 CLI、框架适配器和特殊嵌入场景使用。普通业务调用不应直接操作
`AgentLoop`，否则需要自行承担 Session 同步和恢复正确性。

`RuntimePolicy` 是调用方给 config 设置的不可绕过上限。config 可以继续收紧，不能重新启用被 policy
禁止的扩展管理或 MCP transport，也不能把 sandbox 降到 policy 下限以下。是否读取管理员预装的
personal Skill 由 policy 决定；Web profile 允许只读发现，但始终关闭扩展管理。CLI 使用
`RuntimePolicy.cli()` 保持本机行为；长期服务应显式传入部署 policy，不要依赖默认值。

## 4. 最小非交互调用

无人值守任务使用安全默认交互端口。它会拒绝授权、停止额外续跑、拒绝定义变化，并在不确定副作用
恢复时选择 abort。

```python
from assistant_agent.interaction import SafeDefaultInteractionPort

session = service.create_session(
    interaction=SafeDefaultInteractionPort(),
    interactive=False,
)

try:
    execution = session.start_run("读取项目并生成摘要")
    for event in execution.events:
        if event.sensitive:
            continue
        handle_event(event)
finally:
    session.close()
```

注意：需要用户授权或 `ask_user` 的任务在无人值守模式下不会自动放行。调用方应把这视为安全结果，
不能捕获后改成 allow。

## 5. Session 生命周期

### 5.1 创建、载入和查询

```python
session = service.create_session(interaction=port, interactive=True)
session = service.load_session(session_id, interaction=port, interactive=True)

session_list = service.list_sessions()
session_page = service.catalog_sessions(query=None, limit=30, cursor=None)
renamed = service.update_session_metadata(
    session_id,
    title="新的会话标题",
    expected_metadata_version=session_page.items[0].metadata_version,
)
run_list = service.list_runs(session_id=session_id)
unfinished = session.unfinished_runs()
presentations = session.list_presentations()
session_snapshot = session.snapshot()
run_snapshot = session.run_snapshot(run_id)
artifact = session.get_artifact(artifact_id)
artifact = service.get_artifact(session_id, artifact_id)
```

一个 `SessionRuntime` 独占以下状态：

- Conversation/history 和 compaction checkpoint；
- RunControl；
- 会话级权限记忆；
- InteractionPort；
- MCPManager、WebClient、Workspace 和日志资源。

同一 Session 不得创建多个 `SessionRuntime` 并并行运行。调用方应建立以 `session_id` 为键的 Runtime
Registry，并对加载和淘汰操作加锁。

### 5.2 Session catalog 与元数据（M23-R1）

```text
get_session_summary(session_id) -> SessionSummary
catalog_sessions(query=None, limit=30, cursor=None) -> SessionCatalogPage
update_session_metadata(session_id, title, expected_metadata_version) -> SessionSummary
```

`get_session_summary` 直接按 ID 读取权威 Session，并从 RunStore 的目标 Session 索引一次聚合 last_run；
它不调用 catalog、不扫描 Session 目录、不扫描全量 Run 目录，也不逐 Run 调公共 load。不存在返回
`SessionNotFoundError`，Session/Run 存储或 schema 故障返回 `SessionUnavailableError`。

该用例在 Session lifecycle 锁和文档锁内完成旧 Session 迁移、Session 字段读取、last_run 聚合和 strict
DTO 构造。线性化点是锁内 last_run 聚合与 DTO 构造完成的时刻；并发 rename/delete/Session Run save
只能在线性化点之前完成并被 summary 看见，或在线性化点之后执行，不能返回无法对应任一真实时刻的旧
summary。RunStore 的 `.session-index-v1/manifest.json` 通过单文件原子替换选择 generation，manifest
记录每个 Session 的完整 Run ID 集合，ref 记录可校验身份，状态和时间仍来自 checkpoint 双槽。每进程
首次看到一个索引 epoch 时，完整 manifest/ref 集合会与可加载、未 tombstone、Session-scoped 的权威
current/previous 集合核对；自洽遗漏、缺 ref/目录、坏 manifest/ref 或 stale ref 都会锁内重建。重建失败
映射 `SessionUnavailableError`，不得返回错误的 `last_run=None`。健康 epoch 的按 ID 查询只做 O(1)
epoch 判断和目标 Session 查询，不扫描全量 Run 目录。

Run save 依次替换 ref、以 manifest 单文件替换提交索引可见性、再写 checkpoint；三者不是跨文件事务。
索引阶段失败不会生成已提交 checkpoint，checkpoint 失败会留下可检测 stale ref，后续新 epoch 查询或
重启根据权威集合重建。文件 replace 前后均 flush/fsync；POSIX 尽力 fsync 必要父目录，Windows 使用
目标文件 flush/fsync + `os.replace`，不宣称断电或存储控制器故障下的绝对持久性。

`catalog_sessions(query=None, limit=30, cursor=None) -> SessionCatalogPage` 是会话目录的唯一权威
入口。结果按 `(updated_at DESC, id DESC)` 做 keyset 分页；`next_cursor` 是绑定规范化 query 的 opaque
token，可在下一页使用不同 limit，但不能解析、修改或与其他 query 混用。query 最长 200 Unicode code
points，匹配时对 query、title、公开 preview 做 NFKC + casefold。缺省和空 query 都表示不过滤。

公共 DTO 均为 strict、`extra=forbid`、frozen 模型，并由 `assistant_agent.service` 与
`assistant_agent.contracts` 同一对象导出；两处也导出 `SESSION_CONTRACT_VERSION=4`：

```text
LastRunSummary = {id,status,updated_at}
SessionSummary = {
  id,title,title_source,metadata_version,created_at,updated_at,
  message_count,preview,last_run
}
SessionCatalogPage = {items: tuple[SessionSummary,...],next_cursor}
UpdateSessionMetadataRequest = {title,expected_metadata_version}
```

`message_count` 只统计公开 user/assistant，preview 只来自公开消息；last_run 从权威 RunStore 一次聚合，
先用 catalog 共用的公开状态集合过滤未知状态，再取 `(updated_at DESC, id DESC)` 第一项。较新的未知状态
不能遮蔽较旧有效 Run。以上 DTO 不含路径、prompt、reasoning、工具参数/结果、checkpoint、Token 或
Artifact 内容。

`update_session_metadata(session_id, title, expected_metadata_version)` 使用锁内 fresh load 的
`metadata_version` 做 CAS。成功后 title 原值持久化、`title_source=user`、版本加一；冲突绝不能自动重试
覆盖。标题必须为 1..100 code points 且至少含一个非空白字符。稳定错误如下：

| 类型 | `code` | 语义 |
|---|---|---|
| `InvalidSessionQueryError` | `invalid_session_query` | query 类型或长度非法 |
| `InvalidSessionLimitError` | `invalid_session_limit` | limit 不在 1..100 |
| `InvalidSessionCursorError` | `invalid_session_cursor` | cursor 损坏、过期、版本或 query 不匹配 |
| `InvalidSessionMetadataError` | `invalid_session_metadata` | 标题/version 约束非法 |
| `SessionNotFoundError` | `session_not_found` | Session 不存在 |
| `SessionMetadataConflictError` | `session_metadata_conflict` | CAS 冲突；可读 `current_metadata_version` |
| `SessionUnavailableError` | `session_unavailable` | Session schema/存储暂不可用 |

Session schema v2 在首次读取时于文档锁内幂等迁移并原子替换。未知未来 schema fail closed。自动标题
来自第一条 Unicode 空白折叠后非空的公开 user 文本，截断到 80 code points；无该消息时为
`（空会话）`。首条非空 user 首次持久化时自动标题与 metadata_version 同步更新；用户 rename 永不被
后续 Run 保存覆盖。缺失 `schema_version`、显式 v0 或缺失 v1 元数据字段的旧文档都在锁内原子迁移为
v2；非法类型、负数和未来版本 fail closed。

所有 Session 更新和带 Session 的 Run checkpoint 共用短时跨进程 lifecycle 文件锁，并在锁内检查持久
tombstone；Session 更新默认要求目标已存在。删除先发布 tombstone 再级联清理 Run，因此旧 Runtime 的
checkpoint 或终态同步不能复活 Session/Run。普通删除还持有 M22 execution lease 覆盖“检查无活动 Run
到删除提交”的窗口，避免检查后并发启动。锁顺序固定为 Session lifecycle（如适用）-> index lifecycle
-> Run lifecycle -> checkpoint 双槽。lifecycle 短锁使用固定 64 分片限制 `.lock` 文件增长；按 ID
tombstone 不分片并持续保留删除事实。

Windows lifecycle 锁使用 `LK_NBLCK` 识别正常争用，并以 50ms 可中断休眠等待最终取得，不依赖
`LK_LOCK` 的固定短重试窗口，也不为正常短临界区设置 timeout。只有 CPython `msvcrt.locking` 实际返回的
`errno=EACCES` 且无 `winerror` 形态视为争用；`EAGAIN`、`EDEADLK`、WinError 5/36、资源/句柄错误及
未知组合立即原样透传。进程内同 shard 由 `RLock` 串行，同线程重入只在最外层取得/释放 OS 锁；上下文
异常退出或进程终止由句柄关闭释放。POSIX `flock` 和全局锁序保持不变。调用方不应把正常锁等待映射成
冲突或存储失败。

支持 `os.register_at_fork` 的平台在 Python `os.fork` 前冻结 lifecycle 线程锁表：当前线程已持任一锁时
稳定抛 `RuntimeError` 并阻止 fork，其他线程持锁时等待其退出临界区。parent 随后恢复原锁状态，child
重建进程内 `RLock`/thread-local，不能继承重入状态。模块 reload 不重复注册 hook。此保证不扩展到绕过
Python `os.fork` 的原生扩展；Windows 没有该 API，使用 `spawn` 时仍由 OS 文件锁提供跨进程串行。

Run 还有独立的短时跨进程 lifecycle 锁和持久 tombstone。首次 `RunStore.save` 创建 Run，后续 save
轮转 current/previous；单删先发布 Run tombstone 再清理两个槽。此后同 run_id 的 save/create 和 load
稳定失败，list 隐藏该 Run，重复删除返回 `False`，进程重启也不会丢失删除事实。默认 `_delete_run`
仍拒绝 running/paused；`force=True` 允许发布 tombstone，使活动执行器的迟到 checkpoint fail closed。
Run delete/prune/tombstone 与 Session cascade 都在索引锁内幂等清理对应 ref；清 ref 不删除 Run
tombstone。Session cascade 遵守上述统一锁顺序，与 M22 execution lease 和 recovery checkpoint 兼容。

新 Session/Run 时间统一写为 UTC RFC3339 `Z`。旧无时区时间冻结解释为 UTC，不读取机器本地时区；
带 offset 时间先换算为 UTC。Session v1 读取时会原子规范化其持久时间，Run 历史只在公共读取边界
规范化。合法小数秒保留为规范 UTC 小数秒，不能按整秒截断；catalog 排序、cursor key 和 last_run
选择均比较解析后的真实 UTC instant，不比较混合格式字符串。

### 5.3 权威消息 ledger 与安全 fork（M23-R2）

`SessionRuntime.snapshot() -> SessionSnapshot` 返回 Session schema v2 的公开快照：

```text
PublicMessageSnapshot = {
  id, role, created_at, reply_to_message_id, content, artifacts
}
SessionSnapshot = {
  id, schema_version=2, title, title_source, metadata_version,
  created_at, updated_at, messages, artifacts, assistant_messages, fork_created
}
```

所有公开 user/assistant 消息只以 `messages` ledger 为权威，ID 严格匹配
`msg_[a-f0-9]{24}`。user 的 `reply_to_message_id` 固定为 `null`；assistant 必须指向同一
Session 的 user。旧 v0/v1 Session 首次读取时在 lifecycle/document 锁内原子迁移；消息自身存在可信
UTC 时间时保留，否则 `created_at=null`，绝不读取文件 mtime 或以迁移时间补造。模型历史和 compaction
checkpoint 不是公开历史事实源，压缩不得改写 ledger。

绑定源 Session 的公共原语：

```python
forked = session_runtime.fork_session(
    before_user_message_id="msg_0123456789abcdef01234567",
    idempotency_key="opaque-visible-ascii-key",
)
```

边界必须是源 ledger 中的 user；目标只复制该 user 之前的消息，并为所有复制消息生成新的全局唯一 ID，
按 source->target 映射重写 assistant reply。目标不复制 Run、Interaction 或 compaction checkpoint。复制
范围内的 Chart Artifact 会深复制、生成新 artifact ID、重绑定目标 Session/message，`run_id=null`，
`created_at` 使用 fork 提交时间；源 Session、Run 和 Artifact 不变。

幂等键必须是 1..200 个可见 ASCII 字符。相同源 + key + 边界跨 Runtime/进程重启永久返回同一目标；
相同 key 改用其他边界返回冲突。首次创建的 snapshot 为 `fork_created=true`，重放为 `false`，普通
snapshot 为 `null`；API 用它分别映射 HTTP 201/200，不得自行扫描存储判断。目标 Session、Artifact 和
幂等身份以单个完整 Session 文档原子发布；匹配的既有幂等结果损坏时 fail closed，不创建第二目标。

稳定错误：

| 类型 | `code` | 语义 |
|---|---|---|
| `InvalidForkRequestError` | `invalid_fork_request` | message ID 格式非法 |
| `InvalidIdempotencyKeyError` | `invalid_idempotency_key` | key 缺失、长度或字符非法 |
| `UserMessageNotFoundError` | `user_message_not_found` | 非本 Session user 边界；跨 Session 同样返回此码 |
| `IdempotencyConflictError` | `idempotency_conflict` | 同 key 被其他边界复用 |
| `SessionMigrationRequiredError` | `session_migration_required` | 旧历史或幂等结果无法安全迁移 |
| `SessionUnavailableError` | `session_unavailable` | Session/Artifact 存储暂不可用 |

`EVENT_CONTRACT_VERSION` 仍为 1，Run checkpoint 仍为 v6；fork 不创建 Run，也不产生 StepEvent。
`PresentationArtifactRef.run_id` 是向后兼容 nullable 扩展，只有 fork Artifact 使用 `null`。

### 5.4 删除

```python
deleted = service.delete_session(session_id)
```

Session 存在 running/paused Run 时，默认抛出 `SessionRunConflictError`。服务不应为方便删除而隐式
取消或丢弃可恢复 Run。`force=True` 只适合已由产品策略明确确认的数据清理流程；它会先发布持久
tombstone，再级联删除该 Session 所属 Run。已持有旧 Runtime 的调用方可能在继续消费事件时收到
`FileNotFoundError`，但其 Session/Run 写入会 fail closed，内联 Artifact 也随 Run 删除而不可读取。

CLI `assistant-agent sessions --delete <id>` 也调用本节同一公共删除用例，不直接操作 SessionStore。
默认遇到活动 Run 返回稳定冲突；明确传入 `--force` 并确认后采用同一 tombstone 与级联语义。`--config`
可指定用于定位 recovery RunStore 的配置文件。

### 5.5 关闭

```python
session.close()
session.close()  # 幂等
```

关闭会拒绝新 Run、请求取消、唤醒并安全拒绝交互等待，然后关闭 MCP、WebClient、Workspace、受管
进程和 logger。调用方仍负责等待自己创建的工作线程退出。

## 6. Run 生命周期

### 6.1 启动和事件消费

```python
execution = session.start_run("分析失败测试")
print(execution.run_id)
if execution.warning:
    publish_safe_notice(execution.warning)

for event in execution.events:
    publish(event)
```

`execution.events` 是同步、惰性 `Iterator[StepEvent]`：

- 创建 `RunExecution` 不等于任务已经执行完；
- 必须在工作线程中持续消费 Iterator；
- 不要先 `list(events)` 再向客户端一次性发送，这会失去流式能力；
- 正常公共流最后有一个 `kind == "run_terminal"`；
- 调用方提前关闭或放弃 Iterator 时，Agent 会尝试把 Run 安全暂停，而不是误记为 completed。
- `RunExecution.close()`/`RetryRunExecution.close()` 会请求取消；若另一线程正阻塞在 `next()`，关闭会
  延迟到该迭代线程离开底层 Iterator 后完成。在此之前 execution lease 继续有效，`close()` 返回不表示
  worker 已退出或 lease 已释放；宿主必须 join/await 自己拥有的 worker。
- `warning` 不是事件或终态，须经调用方脱敏，不得据此把 Run 标记为 failed/paused/completed。

终态规则：

1. `final` 只表示完整 assistant 正文，不改变 Run 状态；
2. `completed/failed/paused/cancelled` 只通过 `run_terminal` 表达；
3. 正常耗尽的事件 Iterator 必须且只能产生一次 `run_terminal`；
4. failed 终态携带结构化 `failure`；失败前的 `content_delta` 是 partial，不能伪装成 `final`；
5. API 只有收到 `run_terminal` 后才能原子更新 Run snapshot 和最终 Session 状态。

同一 Session 已有活跃 Run 时，`start_run()` 抛 `SessionBusyError`；存在 paused/running 历史 Run 时，
创建新 Run 抛 `SessionRunConflictError`，避免会话历史分叉。

### 6.2 暂停、取消和恢复

```python
session.pause()
session.cancel()

# worker 已退出、checkpoint 为 paused 时：
cancelled = session.cancel_run(run_id)
for event in cancelled.events:
    publish(event)

resumed = session.resume_run(run_id)
assert resumed.run_id == run_id
for event in resumed.events:
    publish(event)
```

- pause：保存可恢复状态；
- cancel：进入不可继续的 cancelled 终态；
- cancel_run：按指定 `run_id` 取消 worker 已退出的 paused Run；重复调用不重复发送 terminal；
- resume：沿用原 `run_id`，校验 provider/model/system prompt/tool schema 变化；
- 定义变化未经 InteractionPort 接受时保持 paused；
- `tool_uncertain` 必须由用户选择 retry/skip/abort，不能默认重放可能有副作用的工具。

Run 达到 completed/failed/cancelled 后，公共门面会先同步 Session，再设置 `session_synced=True`。同步
失败时保留未同步 checkpoint，调用方不要自行伪造成功状态。

Run 的终态和 Session 同步由 Agent 唯一拥有。即使事件源出现未分类异常，API 也不得发布 synthetic
terminal；应继续消费 Agent 返回的结构化 failed terminal。若 Iterator 因调用方自身网络/worker 故障
中断，应重新读取公共 snapshot/恢复 Run，而不是根据 Python 异常文本猜测状态。

## 7. StepEvent 契约

公共类型：

```python
from assistant_agent.service import (
    EVENT_CONTRACT_VERSION,
    BudgetSnapshot,
    RunFailure,
    StepEvent,
    ToolDisplay,
)
```

`StepEvent` 完整公共字段：

```text
kind, text, tool_name, tool_args, is_error, usage, call_id, display,
result_code, result_metadata, contract_version, sensitive,
terminal_status, failure, phase, budget, chart
```

新增字段保持可选，调用方必须忽略未知未来字段和未知向后兼容事件，不能使 Run 消费失败。

主要事件：

| kind | 含义 | 调用方建议 |
|---|---|---|
| `content_delta` | 助手文本增量 | 流式追加 |
| `reasoning` | 模型 reasoning | `sensitive=True`，默认丢弃 |
| `tool_call` | 工具调用 | 使用 `call_id` 和 `display` |
| `tool_result` | 工具结果 | 用同一 `call_id` 配对 |
| `usage` | token 使用量 | 展示或统计 |
| `notice` | 非终态通知 | 转成服务通知 |
| `final` | 最终回答 | 落屏，但不要单独判断 Run 终态 |
| `error` | Agent 错误事件 | 脱敏后展示 |
| `interrupted` | 兼容事件 | 终态以 `run_terminal` 为准 |
| `activity` | 安全运行阶段和可选预算快照 | 更新临时 Run 状态，不写入消息历史 |
| `run_terminal` | 公共 Run 终态 | 读取 `terminal_status` 和 `failure` |

成功的 `present_chart` 工具结果在 `chart` 携带完整 `ChartArtifact`。API 事件层只应转发其 ref/summary，
完整数据通过 `get_artifact` 按需读取。其他事件的 `chart` 为 null；旧消费者忽略该字段即可。

### 7.1 Chart DTO

`ChartSpecV1` 字段：

```text
schema_version=1, chart_type, title, description, source_label,
columns[{key,label,data_type,unit}], rows,
x_key, series[{key,label}], category_key, value_key
```

`chart_type` 为 `line|bar|stacked_bar|area|scatter|donut`；`data_type` 为
`string|number|datetime`。line/bar/stacked_bar/area 使用 `x_key+series`；scatter 的 x/series
必须是 number；donut 使用 `category_key+value_key` 且 value 为 number。单元只允许 string、有限
number 或 null，bool 非 number。硬限为 12 列、5000 行、20000 cells、8 series。

`ChartArtifact` 是以下 ref 字段加完整 `spec`：

```text
artifact_id, kind="chart", schema_version=1, content_hash="sha256:...",
session_id, run_id, message_id, created_at, title, size_bytes, spec
```

单 Artifact 最大 512 KiB，每 Run 最多 16 个且合计 2 MiB。Artifact 创建后不可修改；更新必须创建
新 ID。新 Assistant message 具有稳定 `message_id` 和 Artifact refs；旧消息允许 `id=null`、
`artifacts=[]`。`AssistantMessageSnapshot`、`SessionSnapshot`、`RunSnapshot` 和全部 Chart DTO 均从
`assistant_agent.service`/`assistant_agent.contracts` 导出。

读取错误只使用类型化异常：`ArtifactNotFoundError.code == "artifact_not_found"`（404）和
`ArtifactUnavailableError.code == "artifact_unavailable"`（503），不能向网络返回路径或原始异常。

`terminal_status` 取值：

```text
completed | failed | paused | cancelled
```

`activity.phase` 合法值：

```text
preparing_context | calling_model | executing_tool | waiting_interaction |
saving_checkpoint | syncing_session
```

activity 是运行事实，不是 reasoning 摘要。`budget` 为可安全展示的 `BudgetSnapshot`：

```text
iterations_used, iterations_limit
tool_calls_used, tool_calls_limit
tool_output_chars_used, tool_output_chars_limit
```

`RunFailure` 字段：

```text
code, safe_message, retryable, allowed_actions, resource, used, limit,
terminal_status, phase, unknown_side_effect
```

稳定 `code`：

```text
tool_output_budget_exhausted | tool_call_budget_exhausted |
iteration_limit_reached | context_limit_exceeded |
provider_rate_limited | provider_unavailable | provider_timeout |
tool_failed | permission_denied | dependency_unavailable | internal_error
```

稳定 `allowed_actions`：

```text
continue | stop | resume_run | retry_run | start_new_run |
adjust_configuration | inspect_dependency | resolve_uncertain_tool
```

API/Web 只能根据 `code`、`retryable`、`allowed_actions` 和 `unknown_side_effect` 决定行为，不能解析
`safe_message`。`retryable=true` 不表示允许自动重试；`unknown_side_effect=true` 只能进入 recovery
Interaction。普通可纠正的 `tool_result.failure` 可使用 `terminal_status=null`，不得据此提前结束 Run。

安全规则：

1. `event.sensitive` 为 true 时，默认不进入 Web DTO、日志、数据库或重连缓存；
2. 工具展示优先使用 `event.display`，不要向客户端暴露原始 `tool_args`；
3. `result_metadata` 不是默认 Web DTO，只有建立字段白名单后才能传输；
4. API 可增加 `seq/timestamp/session_id/run_id`，但不能写回 Agent checkpoint；
5. 网络层只以 `run_terminal` 判断 Run 终态；
6. `failure.safe_message` 可展示，但第三方原始异常、密钥、环境变量和敏感参数不能进入 DTO；
7. `activity` 不写入 Session history，`reasoning` 不进入普通 Web 事件或重连缓存。

## 8. 跨线程事件桥

Agent 保持同步，上层 async 服务应把整个 Iterator 消费放在受控工作线程中，而不是只对
`start_run()` 调用一次 `asyncio.to_thread()`。

下面是一个带背压的简化桥接示例：

```python
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor


def consume_run(execution, loop, queue) -> None:
    for event in execution.events:
        future = asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        future.result(timeout=10)


async def start_worker(session, task, executor: ThreadPoolExecutor):
    execution = session.start_run(task)
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()
    worker: Future = executor.submit(consume_run, execution, loop, queue)
    return execution.run_id, queue, worker
```

生产实现还应处理：

- worker 异常映射；
- queue 满时的背压和超时策略；
- 每 Run 单调事件序号；
- 先持久化/缓存，后广播；
- 慢 WebSocket 订阅者隔离；
- 客户端断开不自动取消 Run；
- shutdown 时先 `session.close()` 请求取消，再等待 worker；不得在 worker 退出前启动同 Session 的新
  Runtime/Run。阻塞 provider 不响应取消时 lease 必须继续保留，不能为加速 shutdown 强行释放。

## 9. 交互端口

需要 Web/UI 交互时使用 `BlockingInteractionPort`：

```python
from assistant_agent.interaction import BlockingInteractionPort

port = BlockingInteractionPort(timeout=60.0)
session = service.create_session(interaction=port, interactive=True)
```

Agent 工作线程会在以下交互上有界等待：

- `approval`：工具权限；
- `question`：`ask_user` 澄清；
- `continue`：达到迭代、工具调用或累计工具输出预算后继续或停止；
- `definition_change`：恢复时定义变化确认；
- `recovery`：不确定副作用的 retry/skip/abort。

另一线程或 async broker 读取请求：

```python
request = port.next_request(timeout=1.0)
if request is not None:
    publish_interaction_request(request)
```

收到用户响应后，按请求 kind 构造对应 Decision：

```python
from assistant_agent.interaction import (
    ApprovalDecision,
    ContinueDecision,
    DefinitionChangeDecision,
    QuestionAnswer,
    RecoveryDecision,
)

port.respond(ApprovalDecision(request_id, "allow"))
port.respond(QuestionAnswer(request_id, answer="方案 A", available=True))
port.respond(ContinueDecision(request_id, continue_run=False))
port.respond(DefinitionChangeDecision(request_id, accepted=True))
port.respond(RecoveryDecision(request_id, "skip"))
```

`respond()` 返回 false 表示 request ID 错误、响应类型不匹配、请求过期或已经响应。Decision 值必须
来自请求的 `legal_options`；越界值即使通过 Python 运行时构造，也不会使 Agent 授权或进入非法恢复
分支。上层服务应在 DTO 校验层直接拒绝越界值，不能把失败重试成默认允许。

M18 `ContinueRequest` 公共字段：

```text
request_id, run_id, session_id, call_id, kind="continue",
reason, resource, used, limit, suggested_increment, hard_limit,
extension_count, max_extensions, legal_options=[continue, stop]
```

`reason` 为 `iteration_limit_reached`、`tool_call_budget_exhausted` 或
`tool_output_budget_exhausted`；`resource` 为 `iterations`、`tool_calls` 或 `tool_output`。响应仍为
`ContinueDecision(request_id, continue_run)`。默认 stop；超时、断线、Runtime close、异常、错误或重复
request ID 均不得继续。continue 只增加当前 Run 预算，决策和新 limit 由 Agent 写入 checkpoint；API
不得自行计算预算、修改 checkpoint 或在网络重连时重复提交响应。

交互请求中的展示目标已脱敏，但调用方仍应使用 DTO 白名单；不要把完整配置、环境变量、system
prompt 或原始工具参数附加到网络响应中。

## 10. Runtime 启动、notice 与异常

低层宿主需要展示 Runtime 创建进度时，可使用：

```python
from assistant_agent.contracts import RuntimeStartupEvent
from assistant_agent.service import create_runtime

def on_startup(event: RuntimeStartupEvent) -> None:
    publish_startup_phase(event.phase, event.status, event.message)

runtime = create_runtime(
    config_path=config_path,
    workspace_root=workspace_root,
    interactive=False,
    startup_observer=on_startup,
)
```

合法 `phase` 为 `loading_config`、`starting_workspace`、`discovering_skills`、`starting_web`、
`preparing_mcp`、`creating_loop`、`ready`；`status` 为 `started`、`completed` 或 `failed`。`failed`
只表示当前创建阶段失败，不携带原始异常。事件没有
session_id/run_id/seq/timestamp，这些网络字段由 API 自行增加。`AgentService` 的高层 Session 工厂当前
不转发 observer；需要启动进度的宿主可在受控装配层使用低层工厂，但不得因此自行复制 Session/Run
状态机。

启动通知位于：

```python
notices = session.runtime.notices
for notice in notices:
    print(notice.code, notice.level, notice.message, notice.details)
```

notice 可能报告未信任 Skill 被跳过、MCP warning、容器外能力或上下文不足。它不是异常，不要求调用方
解析终端文本。

结构化能力快照位于：

```python
capabilities = session.capabilities
capabilities = service.probe_capabilities()  # 一次性探测，结束后自动关闭 Runtime
```

快照包含 sandbox、工具名、Skill 指纹和 MCP server 的状态，不包含 header、env、完整命令、原始异常、
工具原始 Schema 或 Skill 正文。MCP 状态完整集合为：

```text
disabled | blocked_by_policy | discovering | available_cached | restart_required |
connecting | connected | degraded_timeout | degraded_connection |
degraded_discovery | required_failed
```

- `available_cached`：当前 Runtime 已从目录注册工具，server 尚未连接，首次调用时连接；
- `discovering`：当前 Runtime 不注册该 server 工具，后台正在发现目录；
- `restart_required`：目录已发现，需创建新 Runtime 才会注册工具；
- `connecting`：首次调用正在连接；
- `connected`：当前 Runtime 已建立连接；
- `degraded_*` / `required_failed` / `blocked_by_policy` / `disabled`：当前不可用。

调用方应映射枚举，不要解析 notice 文本推断能力，也不要把 `available_cached` 当作 provider readiness。

公共异常：

| 异常 | 含义 | 常见服务映射 |
|---|---|---|
| `RuntimeConfigError` | 配置无效 | 启动失败或 503 |
| `RuntimeInitializationError` | Runtime 某阶段启动失败 | 503，记录 `stage` |
| `RuntimePolicyError` | config 试图突破部署 policy | 部署错误或 503 |
| `RuntimeDependencyError` | required MCP 不可用 | 503 capability_unavailable |
| `RuntimeClosedError` | Runtime 已关闭 | 409/410 |
| `SessionBusyError` | Session 已有未完成 Run | 409 `session_busy` |
| `RunStillActiveError` | 跨进程 Session execution lease 已被持有 | 409 `run_still_active` |
| `RunNotFoundError` | Run 不存在 | 404 `run_not_found` |
| `RunNotResumableError` | 非 paused Run 请求恢复 | 409 `run_not_resumable` |
| `RunNotReconcilableError` | 非遗留 running Run 请求协调 | 409 `run_not_reconcilable` |
| `RunNotRetryableError` | Run 不满足安全重试条件 | 409 `run_not_retryable` |
| `RunRecoveryRequiredError` | 存在 uncertain side effect | 409 `run_recovery_required` |
| `IdempotencyConflictError` | 幂等键已用于不同重试请求 | 409 `idempotency_conflict` |
| `SessionRunConflictError` | 未完成 Run 冲突或归属错误 | 409 `session_run_conflict` |

不要把异常 cause、配置内容、密钥或完整工具参数直接返回客户端。服务日志记录异常类型、阶段、
session_id/run_id 和内部 trace 即可。

## 11. 并发与资源所有权

推荐结构：

```text
AgentService（一个服务实例）
  -> SessionRuntime Registry
       -> session-a: Runtime + InteractionPort + 最多一个 Run worker
       -> session-b: Runtime + InteractionPort + 最多一个 Run worker
```

- 不同 Session 可以在不同工作线程并行；
- 同一 Session 最多一个 Run worker；
- ThreadPool 必须有界，不能每个请求无限创建线程；
- Runtime Registry 只能淘汰无活跃 Run、无交互等待的 Session；
- 进程关闭时停止接收新请求，关闭所有 SessionRuntime，再 join worker；
- 一个 Runtime 不能在多个 Session 间共享权限记忆、Conversation 或 MCP 状态。
- optional MCP 离线不影响服务 liveness/readiness；required MCP 只影响正在创建的 Runtime；
- required MCP 在创建期同步校验；optional MCP 不应进入服务启动 readiness 门；
- optional MCP 无目录时后台发现并在完成后关闭探测连接，当前 Runtime 不热插拔新工具；
- active/paused Runtime 不热插拔工具；能力恢复后只重建无未完成 Run 的空闲 Runtime；
- Runtime 重建使用原 session_id 调用 `load_session()`，并安全清空内存授权记忆。
- 每个 SessionRuntime 的受管后台进程 registry 独立；Runtime 淘汰、服务 shutdown 或初始化回滚必须
  关闭 registry，不能只停止 Run worker。

状态默认写入：

```text
%USERPROFILE%/.assistant_agent/workspaces/<workspace-id>/
  sessions/
  runs/
  logs/
  artifacts/
  mcp-stderr/
  cache/mcp-tools/
```

可通过 `ASSISTANT_AGENT_HOME` 改变用户级状态根目录。M22 起，同一台机器共享状态目录的多个服务进程
由 OS 文件锁保证同一 Session 最多一个执行者；锁由执行 Iterator 持有到退出，进程崩溃后由 OS 释放。
多节点共享存储仍需要外部原子租约/CAS，不能依赖本地文件锁。

## 12. Web API 参考映射

推荐把 REST 作为控制面，把 WebSocket/SSE 作为事件面：

```text
POST   /sessions
GET    /sessions/{session_id}
DELETE /sessions/{session_id}
GET    /sessions/{session_id}/artifacts/{artifact_id}
POST   /sessions/{session_id}/runs
GET    /runs/{run_id}
POST   /runs/{run_id}/pause
POST   /runs/{run_id}/cancel
POST   /runs/{run_id}/resume
POST   /runs/{run_id}/reconcile
POST   /runs/{run_id}/retry
POST   /runs/{run_id}/interactions/{request_id}/responses
GET/WS /runs/{run_id}/events?after=<seq>
```

建议：

- 启动 Run 返回 202 和 `run_id`，不要等待任务完成；
- WebSocket 断开不取消 Run；
- 事件先进入有界重连缓存，再广播；
- 重连按 `after_seq` 补发，缓存缺口返回明确 reset_required；
- 授权响应使用独立、已认证的命令接口；
- 长期 Bearer Token 不放在 WebSocket URL；
- 服务生成的 heartbeat/activity 必须标注为服务事件，不能伪装成 Agent reasoning。

Agent 事件推荐映射：

```text
activity            -> run.activity（覆盖临时状态）
content_delta       -> assistant.delta（partial=true）
tool_call/result    -> run.tool（按 call_id 配对，优先 display）
tool_result(chart)  -> assistant.artifact（只发 ref/summary，完整数据走 REST）
usage               -> run.usage
final               -> assistant.final_candidate
error/interrupted   -> run.notice（不是终态）
run_terminal        -> run.terminal + 原子更新 Run snapshot
```

Run snapshot 至少保留 `terminal_status/failure/current_phase/budget/pending_interaction/final_candidate/artifacts`。
API 自行增加 seq、timestamp、session_id、run_id、heartbeat 和重连缓存；网络重连只能重放 API 已缓存
DTO，不能重新迭代 Agent 或重新执行工具。

活跃 `BlockingInteractionPort` 的 `pending_interaction` 只含 request_id/kind/expires_at/call_id，不含问题
正文、工具参数或授权目标。进程重启后旧 worker 和旧 request 均不存在，该字段为 null；持久 running Run
此时走 reconcile，不能复用旧 request ID。

M22 控制语义：`resume_run()` 只接受 paused；API 重启遗留的 running Run 先以 Idempotency-Key 调用
`reconcile_orphaned_run()`，成功后变为 paused。`retry_failed_run()` 只接受 Agent 明确判定为 safe 的
failed Run，始终创建新 Run ID并返回 `RetryRunExecution`；相同原 Run和幂等键返回同一新 Run。
v1-v5 checkpoint 迁移为 `retry_safety=unknown` 且无可靠会话基线，因此旧 Run 默认不允许普通重试。

M24 REST/事件映射：

```text
MessageResponse.id: string | null = null
MessageResponse.artifacts: ArtifactSummaryResponse[] = []
RunResponse.artifacts: ArtifactSummaryResponse[] = []
GET artifact -> AgentService.get_artifact(session_id, artifact_id)
ArtifactNotFoundError -> HTTP 404 {code: "artifact_not_found"}
ArtifactUnavailableError -> HTTP 503 {code: "artifact_unavailable"}
```

`assistant.artifact` 只携带 `PresentationArtifactRef` 等价 summary，不携带 `spec.rows`。Web 收到后再按
session/artifact ID 读取完整 Artifact。API 不复制完整 Artifact；缓存 summary 仅用于事件重连，不成为
权威存储。成功事件序列：

```text
run.tool(call_id, phase=call)
run.tool(call_id, phase=result) + assistant.artifact(summary)
assistant.final_candidate
run.activity(syncing_session)
run.terminal(completed)
```

非法/超限图表序列为 `tool_result(result_code=artifact_rejected) -> run.notice -> 后续正文 ->
run.terminal`，不得把图表局部失败提升为 Run failed。刷新历史时以 `SessionSnapshot.messages` 为
权威，`assistant_messages` 仅为兼容投影；schema v3 消息都有稳定 ID，API 不得生成或补造。删除 Session 后旧
artifact URL 必须返回统一 404，跨 Session 查询也返回同一 404，避免泄漏存在性。

历史 M28 在不改变 Event v1 外壳的前提下，把 `chart` 扩展为按 `schema_version` 判别的
`ChartArtifact | ChartArtifactV2`。V1 canonical JSON/hash 不变；V2 使用受控
`datasets/layout/panels/derivations`，支持 15 种普通图表、多轴、多面板和白名单 overlay。调用方必须：

- 将 artifact/ref DTO 的 `schema_version` 接受范围扩展为 1/2；
- 通过 `RuntimeCapabilities.chart_spec_versions` 发现能力，不按 provider/model 猜测；
- 只把 V2 白名单字段映射为 renderer 配置，禁止透传 option/formatter/HTML/URL/JS/style；
- 直接使用 Agent 产生的 histogram/boxplot/percent derived dataset，不在 API/Web 重算；
- 未知或损坏 V2 只降级当前图表，不丢失文字消息，不改变 final/run_terminal；
- M31-M33 之后，上述历史兼容范围不再适用于当前运行时；当前只接受 Chart V2、Session v5、Run
  checkpoint v9 和 Output v1，API 不复制迁移状态机，旧状态在部署前清理。

M30 保持 Event v1、Session contract v3、RunState v7 和 ChartSpecV2 字段不变，仅收紧 Agent 新建
Heatmap 的模型输入边界：生成的 X/Y 轴均为 `category`，空 rows、全 null value、null/空白分类坐标
和空 derived dataset 返回 `artifact_rejected`。历史 M28 V2 Artifact 继续按原 DTO 读取，不进行破坏性
重写。重复坐标但缺少聚合语义时，首次失败的 `tool_result.result_metadata` 可 additive 包含：

```text
field_path: string                         # aggregate 或 panels[i].aggregate
allowed_values: [count, sum, mean, min, max]
duplicate_coordinate: string[]             # 有界、安全坐标摘要
duplicate_count: integer >= 2
correction_remaining: 0 | 1
```

这些字段仅是安全纠错事实。API/Web 不得解析中文 `text`，不得自动选择 aggregate，也不得因为
`retryable=true` 自动重放工具；模型只可按同一图表意图修正一次。不同图表意图的修正额度相互隔离，
额度从既有 checkpoint 消息账本重建，不增加 checkpoint 字段。`artifact_rejected` 仍只表示图表局部
失败，不能改变文字回答、`final` 或唯一 `run_terminal`。

### 12.1 M31-M33 current-only 覆盖规则

M31 的 hard cut 覆盖本节此前的兼容读取说明：

- 公共根导出 `AGENT_SERVICE_CONTRACT_VERSION = 5`；API 启动时必须校验。
- `ChartSpecV1`、`ChartArtifact`、`PresentationArtifactRef`、`AnyChartArtifact` 和
  `AnyPresentationArtifactRef` 已删除，不再提供 re-export。
- `StepEvent.chart`、Run/Session presentations、公开 message refs 均只接受 V2。
- RunStore 的写入、双槽读取和 Coordinator 恢复只接受 checkpoint v9；v1-v8 不回退、不迁移。
- SessionStore 的读取、catalog、summary、fork 和写入只接受 Session v5；v0-v4 不回写、不迁移。
- UserMessageInput/MessageContent/AttachmentRef 当前只接受 Content/Attachment v1；checkpoint/session 只保存
  ref，不保存附件正文、base64、path 或 EXIF。
- schema 不匹配分别抛出 `unsupported_run_state_schema`、`unsupported_session_schema`、
  `unsupported_chart_schema`，附 `expected_version`/`actual_version`。API 不解析 message。
- 旧测试状态按 [M31 交接](archive/phase22/m31-agent-api-handoff.md)先备份再清理；不得手工篡改版本号。

## 13. 常见错误

- 启动 `python -m assistant_agent` 子进程并解析 stdout；
- 在 FastAPI event loop 直接遍历 `execution.events`；
- 为每条消息重新创建 Runtime，丢失会话授权和 MCP 状态；
- WebSocket 断开时自动 cancel Run；
- 将 `final` 当作唯一终态，忽略 `run_terminal`；
- 解析中文错误文本推断重试、继续或用户按钮；
- 把 `activity` 或 partial `content_delta` 写成完整 assistant 消息；
- 把 `tool_args`、reasoning 或异常堆栈直接发给客户端；
- API 自己复制 `sync_terminal_session`、definition difference 或 recovery 状态机；
- 同一 Session 并发载入两个 Runtime；
- 超时、断线或未知 request ID 时自动授权；
- 未固定事件契约版本就部署调用方。
- 把 `available_cached` 当作 MCP 已在线，或等待所有 optional MCP 才标记 API ready；
- 在 `discovering -> restart_required` 后向活跃 Runtime 热插拔工具。
- 把完整 Chart rows 放入 WebSocket 重连缓存，或让 Web 接收模型生成的 ECharts option；
- API 扫描 Agent Session/Run 文件、复制完整 Artifact，或为旧消息补造不稳定 message ID。
- API 按消息数组位置/文本推断 reply，或自行复制 history/Artifact 实现 fork。

## 14. 接入验收清单

1. 只导入 `assistant_agent.service`、`assistant_agent.contracts` 和必要的 `assistant_agent.interaction` 实现；
2. 启动时验证 `AGENT_SERVICE_CONTRACT_VERSION == 5`、`SESSION_CONTRACT_VERSION == 5`、
   `OUTPUT_CONTRACT_VERSION == 1` 与 `EVENT_CONTRACT_VERSION == 1`；
3. config/workspace 路径由服务端固定；
4. Iterator 在有界工作线程中逐事件消费；
5. reasoning 和原始工具参数不进入网络 DTO；
6. 同 Session 第二个 Run 明确冲突；
7. pause/cancel/resume 保持原 run_id 和终态语义；
8. 五类 Interaction 均能请求、超时和安全拒绝，三类预算 continuation 均按精确 request ID 响应；
9. WebSocket 断线后可按序号重连，不影响 Run；
10. 初始化失败、Session 淘汰和进程 shutdown 后无遗留 MCP/HTTP/受管进程或 worker；
11. 调用方具备事件转换、并发、断线、授权和脱敏测试；
12. `final -> run_terminal(completed)` 顺序稳定，failed/paused 只产生一次带结构化 failure 的终态；
13. Provider 429/503/timeout、预算、权限、依赖和未知副作用不依赖错误文本分类；
14. reasoning、原始异常、密钥、环境变量和敏感工具参数不会进入网络 DTO；
15. Agent 与调用方分别通过各自的 pytest、Ruff 和 mypy 质量门。
16. optional MCP 离线或后台发现不会阻塞 API readiness，required MCP 创建失败仍返回类型化依赖错误；
17. API 能正确展示 `available_cached`、`discovering`、`restart_required`、`connecting`，且不把目录状态
    误报为在线；
18. Runtime 淘汰/关闭后后台发现线程、MCP 子进程和已连接 transport 均被清理。
19. `ToolDisplay.timeout_seconds` 缺失时保持兼容，存在时只作为安全展示值，不读取完整命令推断超时；
20. API 不写死 `manage_process`，并能处理工具因上下文预算或 policy 未注册的 Runtime；
21. API 忽略普通 `tool_result.chart=null`，把成功 chart 映射为 summary 事件并通过 REST 拉完整数据；
22. 图表刷新/历史恢复、跨 Session 404、删除级联、503 损坏态和断线重放均通过；
23. `artifact_rejected` 不改变 final/run_terminal，低上下文或 recovery 关闭导致工具缺失时 API 仍 ready；
24. Web 只把 ChartSpecV2 白名单映射为固定 ECharts option，V1/未知 schema 不迁移、不猜测；
25. API 固定 `SESSION_CONTRACT_VERSION == 5`，保真映射 message ID/time/reply/artifacts/outputs/content parts；
26. fork 首次/重放按 `fork_created` 映射 201/200，同 key 异参、跨 Session 边界和迁移失败均按稳定 code；
27. edit/regenerate 先 fork、再显式创建普通 Run，第二步失败不得再次隐式 fork；
21. opaque process ID 不作为 OS PID 展示、不跨 Runtime 持久化，Runtime 淘汰/关闭后不自动恢复；
22. `background_process_detected`、`managed_process_container_unsupported` 和
    `managed_process_detached_child` 按结构化工具结果处理，不解析中文文本；
23. Session 淘汰、初始化失败回滚和服务 shutdown 后，Runtime 拥有的后台进程均被终止。

`assistant_agent_api` 的最新交接见
[archive/phase14/m21-agent-api-handoff.md](archive/phase14/m21-agent-api-handoff.md)；M20 启动交接见
[archive/phase13/m20-agent-api-handoff.md](archive/phase13/m20-agent-api-handoff.md)；M19 架构交接见
[archive/phase12/m19-agent-api-handoff.md](archive/phase12/m19-agent-api-handoff.md)，M18 功能交接见
[archive/phase11/m18-agent-api-handoff.md](archive/phase11/m18-agent-api-handoff.md)，M16 初始边界记录见
[archive/phase9/m16-assistant-agent-api-handoff.md](archive/phase9/m16-assistant-agent-api-handoff.md)。这些文件是历史交接，发生冲突时
以本文和安装版本导出的公共 Python 类型为准。
