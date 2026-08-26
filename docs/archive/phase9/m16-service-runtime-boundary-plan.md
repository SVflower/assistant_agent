# M16 Agent 公共服务运行时边界方案

> 状态：已完成（2026-07-17）。前置 M15 已完成、归档并以提交 `4091ef2` 推送。
>
> 内核结论：**预计不修改 `src/assistant_agent/agent/loop.py`**。现有 `continue_check`、
> `recovery_check`、`ToolContext`、`RunCoordinator` 和 `RunControl` 注入点足以实现本方案。
> 若实现时发现必须改变 Loop 状态机，将停止开发，另行说明正确性理由并申请授权。

## 1. 背景与目标

`assistant_agent_api` 将通过安装后的 `assistant-agent` Python 包进程内调用 Agent，不复制源码、
不启动 CLI 子进程，也不解析 Rich 终端输出。API 前置契约见：

`D:\Dev\AI\assistant_agent_api\docs\AGENT_SERVICE_REQUIREMENTS.md`

M16 的目标是把已经存在的运行能力整理成稳定、UI 无关的 Python 服务边界：

1. 单一 Runtime 工厂供 CLI 与 API 共同复用；
2. 用同步、结构化 InteractionPort 替代 Console 字符串回调；
3. 用公共门面统一 Session、Run、恢复和终态同步编排；
4. 冻结可版本化的 `ItemEvent` 进程内契约；
5. 明确每 Session 隔离、单 Run 执行和幂等关闭语义。

本期保持同步 Agent，不进行全栈 async 改造，也不把 HTTP/API 层职责带入 Agent 仓库。

## 2. 现状调研

| 领域 | 当前实现 | 缺口与风险 |
|---|---|---|
| Runtime 装配 | `cli/setup.py` 449 行，已统一创建 LLM、Workspace、Skills、MCP、Web、Tools、Loop 与日志 | 直接依赖 Typer/Console；打印和退出混在装配中；大量路径取 `Path.cwd()`；API 无稳定入口 |
| 初始化回滚 | `build_runtime()` 失败时关闭 MCP、Web、Workspace/进程并结束日志 | 逻辑可复用，但异常未类型化；Interaction 等待者不在生命周期内 |
| Runtime 关闭 | `Runtime.close()` 已有 `_closed` 幂等门 | 未关闭交互端口或先取消活跃 Run；类型注解只写 `NullLogger`，实际也可能是 `EventLogger` |
| Skill 信任 | 启动时通过 Console 聚合授权后才注入未信任 Skill | 服务初始化不能等待输入；需默认只注入显式受信 Skill，并用结构化 notice 报告跳过项 |
| 工具权限 | `PermissionRequest`、策略、精确/上级 scope、记忆、审计、approval checkpoint 已完备 | `ToolContext` 最终只调用字符串 callback；授权请求发生时 `current_call_id` 尚未绑定 |
| `ask_user` | `AskUserTool` 调用 `ctx.ask` | 工具内部先检查 `sys.stdin.isatty()`，会错误绕过 API 的交互端口 |
| 续跑与恢复 | Loop 已注入 `continue_check` / `recovery_check` | 回调由 Console 拥有，缺少结构化请求和安全超时语义 |
| Session/Run | `SessionStore`、`RunStore`、`RunCoordinator` 状态机成熟 | 创建/载入/执行/同步分散在 `main.py` 和 `cli/recovery.py`，API 会被迫复制编排 |
| Run 清理 | `RunStore.prune()` 只清理 terminal 且 `session_synced=True` 的 Run | 语义正确，应由公共门面调用并保持不变 |
| 事件 | `agent/events.py` 已有 call_id、ToolDisplay、结果代码和 metadata | 缺少契约版本、敏感标记和统一 terminal outcome；`interrupted` 无法单独区分 paused/cancelled |
| 隔离 | Runtime 自有 Conversation、RunControl、ToolContext、MCPManager | 默认状态路径和项目目录仍可能从进程 cwd 推导；同 Session 单活跃 Run 尚无公共门面约束 |
| 架构护栏 | 当前层级到 `main(6)`，尚无 service/interaction 层 | 新层必须进入适应度测试，防止 tools 反向依赖 service 或 service 依赖 CLI/UI |

结论：现有核心状态机与资源组件适合扩展，问题集中在所有权和公共接口，不需要重写 Loop、MCP
或 provider client。

## 3. 范围

### 3.1 必做

- 公共、UI 无关、类型化错误的 Runtime 工厂；
- CLI 迁移到同一工厂，删除旧的第二套装配逻辑；
- 同步 InteractionPort、结构化请求/响应 DTO、安全默认实现及线程阻塞参考实现；
- Session/Run 公共服务门面及每 Session 执行上下文；
- Session CRUD、history/checkpoint 装载、busy 检查、运行、暂停、取消、恢复、同步和 prune；
- 稳定事件公共导出、契约版本、敏感标识及明确终态事件；
- 显式 `config_path` / `workspace_root` 路径传播，不在并发运行中调用 `os.chdir()`；
- 初始化失败与关闭的幂等资源回收；
- CLI/API 契约、并发、错误路径和资源泄漏测试。

### 3.2 可选

- 在包根仅 re-export 最小公共入口；默认优先使用 `assistant_agent.service`，避免扩大顶层命名空间；
- 为公共 DTO 生成静态 API 文档。本期不引入额外 schema 框架。

### 3.3 不做

- FastAPI、Router、HTTP 状态码、WebSocket、Bearer Token、Origin/CORS 或 event ticket；
- asyncio Event Hub、网络事件 seq/timestamp、heartbeat 或重连缓存；
- Runtime Pool、API 并发配额、数据库、Redis、任务队列或多租户；
- Web DTO、前端展示协议或对原始工具参数的二次解释；
- 全栈 async、跨进程 Runtime 或 provider/MCP async 重构；
- 对外暴露隐藏 reasoning、密钥或未经脱敏的参数。

## 4. 分层与公共模块

新增两个边界：

```text
interaction (低层协议/线程同步，不依赖 tools/agent/ui)
    ↑ tools / agent / service / ui

config -> llm/runtime/web -> tools/skills/mcp -> agent -> service -> ui/cli -> main
```

- `assistant_agent.interaction`：Protocol、纯 DTO、安全默认端口、线程安全阻塞端口；DTO 使用稳定字符串
  表示 capability/scope，不反向导入 tools 类型。
- `assistant_agent.service`：Runtime 工厂、Session/Run 门面、公共事件出口和服务异常；允许依赖
  config/llm/runtime/web/tools/skills/mcp/agent/session/obs，但绝不依赖 cli/ui。
- `assistant_agent.cli`：只保留参数解析、Console Interaction adapter 和展示；可依赖 service。
- `assistant_agent.ui`：可实现 Console adapter，但公共层不能 import ui。

架构适应度测试将显式登记 `interaction` 与 `service` 层，并增加“service 不依赖 cli/ui”、
“公共模块可在未安装终端交互依赖的语义下导入”的检查。

## 5. M16a：公共 Runtime 工厂

建议公共入口：

```python
runtime = create_runtime(
    config_path=config_path,
    workspace_root=workspace_root,
    interaction=interaction,
    interactive=True,
    session_id=session_id,
    provider=provider_override,
    max_iterations=max_iterations_override,
)
```

### 5.1 输入与输出

- `config_path`、`workspace_root` 在公共入口显式传入并立即 resolve；服务端不接受单次用户消息覆盖；
- CLI adapter 可先使用现有 `find_config_file()` 决定路径，再调用同一公共入口；
- 返回 `AgentRuntime`，包含 config、loop、logger、stores、skills/MCP/Web/Workspace、RunControl 和
  结构化 `notices`；
- notices 至少含 `code`、`level`、`message`、可选的脱敏 `details`，不得要求解析终端文本；
- 配置失败抛 `RuntimeConfigError`，分阶段初始化失败抛 `RuntimeInitializationError(stage=...)`，
  保留安全 cause 供日志诊断，不调用 `sys.exit` 或 `typer.Exit`。

### 5.2 路径与装配

所有当前隐式 cwd 路径改为从 `workspace_root` 派生：

- `state_paths(workspace_root)`；
- `project_skills_dir(workspace_root)` 及 legacy project skill 目录；
- `resolve_log_dir(..., workspace_root)`、`resolve_run_dir(..., workspace_root)`；
- `SessionStore(paths.sessions)`、MCP service workspace、artifact/stderr 目录；
- logger 的 cwd 字段和 Host/Confined/Container Workspace root。

不修改全局 `os.chdir()`。两个 Runtime 使用不同 root 时，状态、Workspace、MCP、授权和产物互不共享。

### 5.3 Skill 启动信任

- 工厂初始化阶段永不调用 InteractionPort；
- personal 内置受信来源及配置显式信任项按现有规则注入；
- 未显式受信的 project/configured Skill 不注入 prompt，返回 `skill_skipped_untrusted` notice；
- CLI 展示 notices。若要恢复启动期授权体验，应由 CLI 在创建 Runtime 前完成显式信任配置，不能让
  公共工厂根据 TTY 分叉成第二种安全语义。

### 5.4 生命周期

初始化按资源栈登记清理动作，任一阶段失败按逆序回滚。`close()` 顺序为：

1. 原子标记 closing，拒绝新 Run；
2. 请求当前 Run 取消；
3. `interaction.close()` 唤醒所有等待并返回安全拒绝；
4. 关闭 MCP、WebClient、Workspace/ProcessSupervisor；
5. 结束 logger session；
6. 原子标记 closed。

每一步允许重复调用；单个 close 失败不阻止后续资源清理，最终以聚合、脱敏的结构化 notice/logger
报告。Agent Runtime 不拥有 API 工作线程；API 负责等待自己的线程退出，但 Runtime 负责解除其交互等待
和终止受管进程。

## 6. M16b：同步 InteractionPort

### 6.1 协议与 DTO

```python
class InteractionPort(Protocol):
    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision: ...
    def ask_question(self, request: QuestionRequest) -> QuestionAnswer: ...
    def confirm_continue(self, request: ContinueRequest) -> ContinueDecision: ...
    def confirm_definition_change(
        self, request: DefinitionChangeRequest
    ) -> DefinitionChangeDecision: ...
    def decide_recovery(self, request: RecoveryRequest) -> RecoveryDecision: ...
    def close(self) -> None: ...
```

所有请求共有：`request_id`、`run_id`、可选 `session_id/call_id`、`kind`、`legal_options`。

- `ApprovalRequest`：tool、capabilities、脱敏 display targets、risks、精确 scopes、可选 broader scope
  及其 label；合法响应为 deny/allow/always/broader（仅请求允许时出现 broader）；
- `QuestionRequest`：脱敏 question、候选项；响应包含所选项或 unavailable；
- `ContinueRequest`：当前迭代数、上限和合法 stop/continue；
- `DefinitionChangeRequest`：仅含 provider/model/system_prompt/tool_schema 的字段级差异摘要和哈希，
  不暴露完整 system prompt 或 tool schema；
- `RecoveryRequest`：tool、call_id、脱敏 ToolDisplay、retry/skip/abort 和明确的重复副作用风险。

响应带原始 `request_id`，由端口校验；错误 ID、过期响应和重复响应均不能放行。公共 DTO 不包含
reasoning、原始 secrets、未脱敏 arguments 或内部 logger payload。

### 6.2 默认与阻塞实现

- `SafeDefaultInteractionPort`：approval=deny、question=unavailable、continue=stop、definition=reject、
  recovery=abort；所有方法不阻塞；
- `BlockingInteractionPort`：使用 `threading.Condition/Event` 有界等待，提供线程安全 `respond()`；
- 每个 request_id 只接受第一次合法响应；未知、过期、重复或选项越界响应返回 False；
- timeout、close、等待异常一律使用对应安全默认值，不抛出“自动允许”；
- `close()` 唤醒全部 waiter，幂等且关闭后拒绝新请求；不依赖 asyncio/HTTP/FastAPI；
- API 可把 request 发布到自己的事件系统，再由另一线程调用 `respond()`。

### 6.3 现有语义接入

- `ToolContext` 保留 PermissionPolicy、精确授权、broader scope、会话记忆、审计和等待耗时统计；只把
  字符串 callback 替换为 InteractionPort adapter；
- Registry 在 `approval_pending` checkpoint 前绑定 run/session/call 上下文，并在整个授权及执行阶段
  保持同一 call_id，finally 恢复；checkpoint 顺序仍为 approval_pending -> wait -> tool_started；
- `AskUserTool` 删除 `sys.stdin.isatty()` 判断，是否可交互完全由端口和 Runtime 模式决定；
- continue callback 由 Runtime 根据当前 Run 上下文构造 DTO；
- definition change 和 tool_uncertain 由服务门面请求端口，拒绝/超时后 Run 保持 paused；
- Console 实现 `ConsoleInteractionAdapter`，保持现有 CLI 选项、授权记忆和 activity suspend/resume 行为。

## 7. M16c：Session/Run 公共门面

建议分为两级：

- `AgentService`：持有显式路径的 SessionStore/RunStore，提供 Session CRUD 和 `open_session()`；
- `SessionRuntime`：一对一拥有 AgentRuntime、Session、Conversation、RunControl、InteractionPort 和
  会话授权，只允许一个活跃 Run。

### 7.1 Session API

- create/load/list/delete；
- create/load 后由 `SessionRuntime` 把 messages 和 compaction checkpoint 装入 Loop；
- delete 默认拒绝存在 running/paused/unsynced Run 的 Session，除非调用方显式选择已定义的强制策略；
- 不依赖 `SessionStore()` 的 cwd 默认值，所有 store 由工厂路径显式构造。

### 7.2 新 Run

1. 在锁内检查 Runtime open 且无 active Run；否则抛 `SessionBusyError`；
2. `RunControl.reset()`，清空仅属于上次任务的瞬时绑定，但保留该 Session 的授权记忆；
3. 创建 RunCoordinator，绑定 session/logger/interaction identity；
4. 返回同步 `Iterator[ItemEvent]` 并持续执行 Loop；
5. 迭代结束或异常时按 RunState 生成唯一 `run_terminal` 事件；
6. terminal Run 幂等同步 Session，成功后 `mark_session_synced()`，再执行安全 prune；
7. 同步失败保留 `session_synced=False` 和 checkpoint，允许再次恢复补同步；
8. finally 清除 active Run 标识，不清除会话授权。

若调用方提前关闭事件 Iterator，门面请求 pause 并完成 checkpoint，不把“消费者停止读取”误判成 completed。

### 7.3 暂停、取消与恢复

- pause/cancel 只通过本 Session 的 RunControl 请求，保持现有升级语义；
- resume 载入原 RunCoordinator，沿用原 `run_id`，不创建替代 Run；
- 先校验 session 归属和 terminal 状态，再比较 provider/model/system prompt/tool schema 定义；
- 有差异时通过 InteractionPort 显式确认；拒绝/超时保持 paused，不调用 `accept_definitions()`；
- `tool_uncertain` 逐 call_id 请求 retry/skip/abort；abort 保持 paused；
- 完成定义确认后才 `note_resume()` 并执行 Loop；
- 公共门面收编现有 `sync_terminal_session()`，CLI 不再拥有该状态转换；
- prune 继续只删除 terminal 且 session_synced 的历史 Run，绝不隐式删除 active/paused/unsynced Run。

## 8. 稳定事件契约

- `assistant_agent.service.events` 明确 re-export `ItemEvent`、`ToolDisplay`、`EVENT_CONTRACT_VERSION`；
- 初始契约版本为 1；新增可选字段保持向后兼容，删除/改义/改变 kind 必须提升主版本；
- `ItemEvent` 新增默认字段 `contract_version`、`sensitive`、`terminal_status`；现有构造调用保持兼容；
- reasoning 事件在类型层自动标记 `sensitive=True`，API 默认丢弃；其他事件不得携带隐藏 reasoning；
- tool_call/tool_result 延续稳定 call_id 配对，展示优先使用 ToolDisplay；
- 服务门面在每次运行末尾只产生一个 `kind="run_terminal"` 事件，`terminal_status` 为
  completed/failed/paused/cancelled，解决 `interrupted` 歧义；
- 原有 final/error/interrupted 继续保留，避免 CLI 与现有调用方回退；
- API 自行添加 seq、timestamp、session_id、run_id、heartbeat 和网络重连缓存。

## 9. CLI 迁移顺序

1. 先建立 interaction DTO/端口及 Console adapter，保持 CLI 输出不变；
2. 提取公共 Runtime 工厂，`cli/setup.py` 只负责解析可选 config、展示 notices/banner 和映射类型化异常
   到 Typer exit；
3. 建立 AgentService/SessionRuntime，迁移 `main.py` 的 chat/run/session 编排；
4. 迁移 `cli/recovery.py` 的定义确认、uncertain 决策、Session 同步和 prune；
5. 删除 CLI 中已迁移的重复实现，并用行为测试固定现有 CLI 体验。

迁移期间不保留长期 feature flag 或两套装配逻辑；每一步先补公共测试，再切换 CLI 调用方。

## 10. 预计文件

### 新增

- `src/assistant_agent/interaction/__init__.py`
- `src/assistant_agent/interaction/models.py`
- `src/assistant_agent/interaction/ports.py`
- `src/assistant_agent/service/__init__.py`
- `src/assistant_agent/service/runtime.py`
- `src/assistant_agent/service/sessions.py`
- `src/assistant_agent/service/events.py`
- `src/assistant_agent/service/errors.py`
- `tests/test_interaction_port.py`
- `tests/test_service_runtime.py`
- `tests/test_agent_service.py`
- `tests/test_service_contract.py`

### 修改

- `src/assistant_agent/cli/setup.py`：收缩为公共工厂的 CLI adapter；
- `src/assistant_agent/cli/recovery.py`、`src/assistant_agent/main.py`：改用服务门面；
- `src/assistant_agent/ui/console.py` 或新增 `ui/interaction.py`：Console adapter；
- `src/assistant_agent/tools/base.py`、`tools/registry.py`、`tools/ask.py`：结构化交互及 call identity；
- `src/assistant_agent/agent/events.py`：向后兼容的契约字段；
- `src/assistant_agent/config/paths.py`：仅在仍有隐式 cwd 缺口时补显式 root 参数；
- `tests/test_architecture.py` 及相关 CLI/recovery/session/runtime 回归测试；
- 完成时更新 `README.md`、`ROADMAP.md`、`AGENTS.md`、`CLAUDE.md`、`docs/TECH_DEBT.md`。

### 明确不修改

- `src/assistant_agent/agent/loop.py`；
- provider 协议和 MCP transport 状态机；
- API 仓库中的 FastAPI/WebSocket/认证/事件缓存实现。

文件拆分是职责边界草案，不以 300 行软线机械拆分；实现时优先保证协议、装配和服务编排各自高内聚，
并确保任何文件不触发 500 行硬线。

## 11. 测试计划

### Runtime 与资源

- fake provider 下显式 config/workspace 创建 Runtime，不导入 cli/ui；
- 每个初始化阶段注入失败，验证已创建资源逆序关闭且二次 close 无副作用；
- MCP thread/session、WebClient、Workspace 和 ProcessSupervisor 无遗留资源；
- 两个不同 workspace Runtime 的状态路径、Conversation、RunControl、权限集合和 MCP manager 不同；
- Runtime close 唤醒交互等待、取消受管进程并拒绝后续 Run；
- 未信任 project/configured Skill 不注入 prompt，只返回脱敏 notice，初始化不等待输入。

### Interaction

- SafeDefault 对五类请求全部采取安全结果；
- Blocking port 可由另一线程响应 allow/deny；
- timeout、close、异常、错误 request_id、重复响应、非法 option 均不放行；
- Approval DTO 含 run/session/call、capability、脱敏目标、风险、精确和 broader scope；
- 权限 grant 和记忆、审计、approval checkpoint 顺序不回退；
- ask_user、continue、definition change、tool_uncertain 在无 Console/无 TTY 环境可工作。

### Session/Run

- Fake provider：create Session -> create Run -> ItemEvent -> terminal sync -> session_synced；
- 同 Session 并发第二个 Run 抛 busy；不同 Session 可在不同线程同时执行且状态不污染；
- 每个新 Run 前 RunControl reset；pause/cancel 状态和 checkpoint 与现有语义一致；
- resume 使用原 run_id；定义拒绝或超时保持 paused；接受后才更新定义；
- uncertain retry/skip/abort 分支及重复副作用提示；
- Session 同步失败可重试且不提前 mark_session_synced；
- prune 不删除 active、paused 或 unsynced Run；提前关闭 iterator 进入 paused 而非 completed。

### 事件与兼容

- call/result 的 call_id 配对；ToolDisplay 脱敏摘要优先；
- reasoning 自动 sensitive；终态严格且唯一映射四种状态；
- 旧 ItemEvent 构造方式仍可用，新增字段有默认值；
- CLI normal/verbose/quiet、banner、活动动画、授权和恢复行为不回退；
- API 契约测试只导入 `assistant_agent.service` / `assistant_agent.interaction`。

最终执行全量 `pytest --cov`、`ruff format --check .`、`ruff check .`、`mypy` 和架构适应度测试。

## 12. 验收映射

| API 契约验收项 | M16 验证 |
|---|---|
| 1. 公共门面完成 Session -> Run -> Event -> 同步 | AgentService fake provider 端到端测试 |
| 2. 另一线程提交授权结果 | BlockingInteractionPort 并发测试 |
| 3. timeout/错误 ID/重复响应不放行 | 端口负向矩阵测试 |
| 4. 五类交互不依赖 Console | 无 cli/ui import 的服务测试 |
| 5. pause/cancel/resume 保持语义 | RunState/checkpoint 回归测试 |
| 6. 恢复沿用 run_id | resume 身份断言 |
| 7. 两个 Runtime 状态不污染 | 双线程双 Runtime 隔离测试 |
| 8. init/close 无资源遗留 | 分阶段故障注入和资源探针 |
| 9. CLI 不回退 | 现有 CLI 全量测试 + adapter 集成测试 |
| 10. API 不导入 cli/ui | 架构测试和独立导入测试 |
| 11. 全部质量门通过 | DoD 实测记录 |

## 13. 风险与控制

- **交互等待与关闭竞态**：request 注册和 close 使用同一锁；close 先标记关闭再唤醒，响应必须匹配
  当前 pending request；测试 timeout/response/close 的交错顺序。
- **Iterator 未消费完导致锁不释放**：服务 iterator 使用 try/finally；提前 close 请求 pause、提交 checkpoint
  并释放 active-run 标识。
- **终态重复同步**：先原子保存 Session，成功后再 `mark_session_synced()`；重复调用按已有状态幂等。
- **Runtime close 与工具执行竞态**：先 cancel + 唤醒 interaction，再关闭受管资源；API 仍负责 join 自己的
  工作线程，文档明确所有权。
- **Skill 行为变化**：服务默认 trusted-only；CLI 对跳过项明确展示 notice，不在初始化阶段静默放行。
- **定义差异泄密**：交互 DTO 只给字段、摘要和哈希，不交付完整 system prompt/schema。
- **公共 API 过早膨胀**：只冻结契约要求的入口，内部 helper 不从 `service.__init__` 导出。
- **迁移双轨漂移**：CLI 切换完成后删除旧装配/同步函数，以测试证明只有一个实现。
- **事件终态兼容**：保留旧事件并新增服务级 terminal 事件，不修改 Loop 既有 yield 顺序。
- **架构层次失真**：interaction 保持无内部上层依赖，service 是编排层；不让 tools 导入 service。

## 14. 完成定义

1. 上述 11 项验收全部通过；
2. CLI 和 API 使用同一 Runtime 工厂及同一 Session/Run 状态编排；
3. 公共层不依赖 Console、Typer、Rich、Questionary、Prompt Toolkit、HTTP、WebSocket、asyncio 或
   FastAPI；
4. 无密钥、隐藏 reasoning、未经脱敏参数进入交互/事件展示 DTO；
5. 不修改 `agent/loop.py`；若该前提失效，必须重新走用户确认；
6. `pytest --cov`、Ruff、mypy、架构测试全绿，并以实测数字更新状态文档；
7. 新技术债登记，方案完成后归档，提交前审查 staged diff。

## 15. 实施结果

- 新增 `assistant_agent.interaction`：五类结构化请求/响应、安全默认端口和线程安全有界阻塞端口；
- 新增 `assistant_agent.service`：UI 无关 Runtime 工厂、类型化异常、结构化 notices、Session/Run
  门面和版本化事件出口；
- CLI Runtime、chat 和带 Session 的 resume 已复用公共工厂/门面，未保留第二套装配；
- 相对日志、Run、Skill、SkillManager 和 MCP cwd 统一相对固定 workspace_root 解析；
- reasoning 明确标记 sensitive，公共流新增唯一 `run_terminal` 和四种无歧义 terminal_status；
- 未信任 project/configured Skill 在服务启动时不等待输入、不注入模型，并返回结构化 notice；
- 完整 API 接入说明见同目录 `m16-assistant-agent-api-handoff.md`；
- 未修改 `src/assistant_agent/agent/loop.py`；
- 验收结果：566 passed、5 skipped、覆盖率 83%，Ruff、mypy、架构适应度测试全绿。
