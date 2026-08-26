# M17 与 assistant_agent_api 协同开发说明

> 状态：Agent M17 已完成；供 `assistant_agent_api` 升级依赖并实施适配。
>
> 日期：2026-07-18。
>
> 旧依赖基线：Agent M16 commit `30efde2`；API 应在 M17 提交后升级到新的不可变 commit/tag。

## 1. 协同结论

Agent M17 将补齐生产 Runtime 策略、MCP 有界降级启动和结构化能力快照。API 不需要等待 M17 才能
继续完善现有 A1-A6，但不能提前猜测或复制 M17 的公共类型。

双方继续遵守以下边界：

```text
assistant_agent
  Runtime 策略、Runtime/Session/Run、InteractionPort、MCP 生命周期、能力快照、ItemEvent

assistant_agent_api
  ASGI 生命周期、Runtime Pool、工作线程、REST/WebSocket、事件缓存、网络 DTO、服务级配额
```

当前只采用“单一部署所有者”模型：一份固定 config、一份固定 workspace_root、一个服务策略、多个隔离
SessionRuntime。本期不增加账号、用户表、tenant、owner_id、RBAC 或 Session ownership 占位字段。

## 2. API 现在应立即完成

### 2.1 修正文档事件契约

`docs/API_PROTOCOL.md` 当前写成：

```text
final -> assistant.final + run.completed
```

应改为：

```text
content_delta -> assistant.delta
final -> assistant.final
run_terminal(completed) -> run.completed
run_terminal(failed) -> run.failed
run_terminal(paused) -> run.paused
run_terminal(cancelled) -> run.cancelled
```

`final` 只代表最终回答正文，不代表 Run 已完成。Run 可能在 final 之后的 Session 同步或 checkpoint 阶段
失败；网络层、Event Hub、WebSocket 关闭和并发容量释放都必须只以 `run_terminal` 为终态依据。

API 当前实现已基本遵守该规则，需把文档和契约测试同步到真实实现，并增加“final 不关闭事件流”的
回归测试。

### 2.2 固化 A1-A6 基线

API 仓库当前尚无初始提交。应先审查并提交已完成的 A1-A6，记录：

- 依赖 Agent commit `30efde2`；
- 69 个测试、87% 覆盖率的实测基线；
- 当前 OpenAPI artifact；
- 当前技术债和协议版本。

不要把现有 API 基线、事件契约文档修正和后续 M17 适配混进一个不可审查的大提交。

### 2.3 保持部署参数不可由请求覆盖

- `config_path` 和 `workspace_root` 只来自 API 启动配置；
- HTTP/WS 请求不得传入 provider key、MCP URL/header/env/command 或任意配置路径；
- 当前 Bearer 只表示部署访问令牌，不扩展为账号、角色或租户身份；
- API lifespan 只创建轻量 `AgentService`，SessionRuntime 继续 lazy create/load；
- Runtime 创建、MCP 连接和同步 Agent Run 继续放入有界工作线程，不能阻塞 ASGI event loop。

### 2.4 先预留内部适配点，不预造 Agent 类型

API 可以在自身端口层预留：

- Session Runtime 的 capabilities 读取入口；
- `capability_unavailable` 错误映射位置；
- 空闲 Runtime 重建操作；
- optional dependency degraded 的展示字段。

这些公共类型现已由 `assistant_agent.service` 导出。API 应先固定包含 M17 的不可变 commit/tag，再从
公共模块导入；不要 vendor 类型，也不要解析 notice 文本推断 MCP 状态。

## 3. M17 已交付的公共契约

以下行为已由 Agent 公共契约提供，API 可以在固定 M17 commit/tag 后开始适配：

- 向 `AgentService` 注入服务端 `RuntimePolicy`；
- 禁止扩展管理工具和服务器 personal Skill；
- MCP transport allowlist 与 sandbox 下限校验；
- required/optional MCP 的结构化启动结果；
- 连接超时与工具调用超时的独立配置；
- `RuntimeCapabilities` 的 Web DTO 映射；
- `RuntimeDependencyError` 的稳定 HTTP 错误映射；
- 一次性 MCP 依赖探测和空闲 Runtime 能力刷新。

API 不要跟随 Agent 工作区或可变 `main`。先保留 `30efde2` 完成 A1-A6 基线提交，再在独立适配提交中
升级到包含 M17 的明确 commit/tag。

## 4. M17 发布后的 API 适配

### 4.1 Service RuntimePolicy

API 启动时构造一个部署级、不可由请求放宽的策略，并传给 `AgentService`。建议生产默认值：

```text
allow_extension_management = false
allow_personal_skills = false
allowed_mcp_transports = 部署显式 allowlist
minimum_sandbox = 部署要求（公网不可信输入建议 container）
```

同一 API 部署共享该 policy；每个 SessionRuntime 仍保持 Conversation、RunControl、授权记忆和 MCPManager
隔离。API 不应重新实现 Agent 的 policy 校验。

### 4.2 能力状态

Runtime Pool 的 hot-session entry 保存 Agent 返回的只读能力快照。API 可将其暴露在 Session 响应或
独立 capability endpoint，但网络 DTO 只映射公共脱敏字段：

- sandbox 等级；
- builtin tool 名称；
- Skill 名称、来源和版本摘要；
- MCP server 名、transport、required/optional、状态、安全错误分类和工具数量；
- extension management 是否启用。

不得返回 headers、env、token、完整 command、原始异常、原始工具 schema、Skill 全文或 reasoning。

### 4.3 MCP 启动和健康语义

- `/health/live`：只表示 API 进程和事件循环存活，不探测 MCP；
- `/health/ready`：表示配置、状态目录、AgentService 和 Runtime Pool 可用，不要求 optional MCP 在线；
- optional MCP 失败：Session 创建成功，capabilities 标记 degraded，并发送结构化 notice；
- required MCP 失败：只使当前 SessionRuntime 创建失败，映射 `503 capability_unavailable`；
- 非法部署配置或 policy 冲突：映射稳定的部署错误，不泄漏原始异常；
- 不因单个 MCP 离线关闭 API 进程或把 liveness 置为失败。

### 4.4 Runtime 刷新

API 负责探测调度、退避、jitter 和全局并发限制。Agent 只执行一次性探测：

1. MCP 恢复后，将对应 Session 标记为“可刷新”；
2. 仅当 Session 没有 active 或 paused Run 时，关闭旧 Runtime；
3. 使用原 session_id 调用 `load_session()` 创建新 Runtime；
4. 更新 capability snapshot；
5. 接受会话授权记忆因重建被清空，这是安全收紧。

禁止向 active/paused Runtime 热插拔工具。paused Run 的工具定义变化继续走 M16 的
definition-change Interaction，不在 API 中绕过。

### 4.5 服务级资源限制

在单一部署所有者模型下，API 应提供部署级限制，而不是用户级配额：

- `max_hot_sessions`；
- `max_concurrent_runs` 和 worker 数；
- Runtime 创建并发数；
- stdio MCP 子进程总量；
- 空闲 Runtime TTL；
- Event Hub 每 Run 缓存和订阅者上限；
- Interaction 等待数量和超时。

API 不建立跨 Session MCP 连接池。首期每 Session 独立 MCPManager，资源压力先由 Runtime Pool 和配额
控制，避免共享 server session、取消状态和凭据边界。

## 5. 双方联调顺序

1. API 修正事件协议文档和回归测试，提交 A1-A6 基线。
2. Agent 独立完成 M17a，并保证 M16 API 调用兼容。
3. Agent 完成 M17b/M17c、公共导出、测试和调用文档，发布不可变 commit/tag。
4. API 升级依赖，只从 `assistant_agent.service` 和 `assistant_agent.interaction` 公共模块导入。
5. API 增加 policy、capability、dependency error 和 idle refresh 适配。
6. 双仓使用 Fake provider 完成联合验收，再执行 optional/required MCP 故障注入测试。

## 6. 联合验收清单

- `final` 只产生 `assistant.final`，只有 `run_terminal` 产生并关闭网络终态；
- optional MCP 全部离线时，API live/ready 正常，Session 可创建并进行普通对话；
- required MCP 离线只返回当前 Session 的 `503 capability_unavailable`；
- Session 创建不阻塞 ASGI event loop，且受 Runtime 创建并发限制；
- 请求无法覆盖 config、workspace、policy、MCP endpoint 或 secret；
- capabilities 全程脱敏，reasoning 不进入网络事件；
- active/paused Run 不发生工具热插拔；
- idle rebuild 保留 session_id/history，清空内存授权并更新能力快照；
- 两个 Session 的 Conversation、RunControl、Interaction 和 MCP 状态互不污染；
- shutdown 和初始化失败后无遗留 worker、MCP 子进程、WebClient 或 Interaction 等待者；
- API 不导入 Agent 的 CLI、UI 或 private 模块；
- 两仓 pytest、coverage、Ruff、mypy 和各自架构测试全绿。

## 7. 明确不做

本轮双方都不实现：

- 账号、注册、用户数据库、租户、RBAC、Session ownership；
- 用户自带 MCP、个人 OAuth、动态 SecretResolver；
- 请求级 config/workspace/MCP 覆盖；
- 跨 Session MCP 连接池；
- active/paused Runtime 工具热插拔；
- Redis、多实例 lease 或任务队列；
- Agent 全栈 async 改造；
- API 仓库复制 Agent 的恢复、权限或 checkpoint 状态机。

## 8. 给 API 项目 AI 的执行约束

先执行第 2 节的 API 内部修正并提交 A1-A6 基线，再固定包含 M17 的不可变 commit/tag，按第 3、4 节
实施独立适配提交。发现公共契约不足时，把需求反馈到 Agent 仓库，不导入 private 模块绕过。
