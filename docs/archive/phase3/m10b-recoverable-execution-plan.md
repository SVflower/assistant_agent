# M10b 计划：步骤级 Checkpoint 与可恢复执行

> 状态：已完成（2026-07-15）。上位规划见 [第三阶段规划](../../phase3-trustworthy-agent-plan.md)。
> 用户已单独确认内核改动；实现与验收完成后归档于 `docs/archive/phase3/`。

## 1. 目标

让一次 Agent 任务在进程退出、模型连接中断或工具执行边界故障后，能够从持久化的明确状态继续，
而不是丢失整轮轨迹或静默重复副作用。M10b 交付的是单机、本地、同步运行时的恢复协议，不是分布式
工作流引擎，也不承诺外部系统的 exactly-once。

完成后必须满足：模型输出的工具计划不因重启丢失；已确认完成的工具不重放；执行结果未知的副作用
工具必须由用户决定；预算、重复熔断、权限授权和摘要 checkpoint 恢复后保持原语义；旧 Session 和
无 Run checkpoint 的普通使用路径继续兼容。

## 2. 调研结论

本期参考成熟框架提炼边界，不引入它们的整套运行时：

1. [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
   与 [persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 把 thread checkpoint 和
   跨 thread store 分开，在 step/super-step 边界保存完整状态。故障恢复从最近成功边界继续，并保留
   同一 super-step 内已成功的 pending writes。本项目对应地把 RunState 与 Session 分文件、分职责。
2. [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) 明确：恢复通常从
   节点开头重跑，interrupt 前的副作用必须幂等，或拆到独立节点。它不能提供任意外部副作用的
   exactly-once。本项目不重跑整个工具节点，而是显式记录 `planned -> started -> completed/failed`；
   `started` 无结果时按权限能力分类，副作用调用必须人工处置。
3. [OpenAI Agents SDK HITL](https://openai.github.io/openai-agents-python/human_in_the_loop/)
   将可序列化 RunState 作为暂停/恢复边界，按稳定 call ID 保存逐调用审批与 sticky decision；恢复仍使用
   原顶层 Agent，并建议给长期挂起状态保存 Agent/SDK 版本标记。本项目据此保存定义指纹、精确权限授权、
   pending approval 和稳定调用 ID，不把审批只存在 Console 调用栈里。
4. [Temporal Activity](https://docs.temporal.io/activities) 与
   [Python error handling](https://docs.temporal.io/develop/python/best-practices/error-handling) 采用
   at-least-once Activity；worker 可能在副作用完成后、结果确认前崩溃，所以 Activity 应幂等并使用稳定
   idempotency key。当前通用 Shell/MCP 无法统一做到幂等，因此本项目只把 call ID 暴露给支持它的工具，
   绝不自动重放未知副作用。
5. [Microsoft Agent Framework checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
   在 super-step 完成后保存 executor state、pending messages 与 pending requests，并提供文件型持久化；
   同时把 checkpoint 存储视为受信边界。本项目只用版本化 JSON，不使用 pickle；路径 confinement、原子
   双槽保存和私有本地目录沿用现有 Session 安全原则。

共同原则：Conversation 是模型上下文，Session 是用户会话历史，RunState 是未完成运行的执行游标；
三者不能混成一个“万能 JSON”。Checkpoint 证明的是框架最后确认的状态，不证明外部世界是否执行成功。

## 3. 现状评估

### 可复用基础

- `AgentLoop` 已有清晰的模型轮次、工具批次、预算和重复熔断边界。
- `Conversation` 能导入/导出 raw history 与 M8b compaction checkpoint。
- `ToolResult.executed/code/is_error`、权限 `PermissionRequest` 和稳定模型 tool call ID 已具备。
- `SessionStore` 已验证 session ID confinement、同目录临时文件、fsync 和 `os.replace`。
- Registry 是权限、预算与 Tool.run 的唯一入口，适合设置“副作用即将开始”和“结果已确认”边界。
- M9c eval 可加入 crash/resume 行为案例；现有 observer 可继续承担安全门和只读审计。

### 当前缺口

- `run()` 每次新建 ToolBudget，iteration、累计预算、last_signature/repeat_count 都只在栈上。
- 模型返回工具调用后，assistant tool_calls 与每个工具结果只存在内存；进程退出会丢失整批状态。
- Registry 在同步 `confirm()` 内等待用户，审批请求和决定未形成可恢复状态。
- 工具副作用完成与结果写回 Conversation 之间没有持久化边界，恢复时无法判断是否应重放。
- chat 只在整轮结束后保存 Session；run 模式完全不保存。没有列出/恢复未完成 Run 的 CLI。
- EventLogger 构造时生成的 `session_id` 与随后创建的 Session 无关；`/clear` 后也不换，run_id/trace_id
  尚未建模。D8 的剩余语义问题应在本期一起还清。
- Session 单文件原子保存能防替换前故障，但 Run checkpoint 还要求“当前槽损坏时回退上一有效槽”。

结论：需要新增独立 `run_state`/`run_store`/`recovery` 模块，并对 Loop 与 Registry 增加窄生命周期接口；
不应把存储 I/O、CLI 选择或 JSON 细节直接塞进 `agent/loop.py`。

## 4. 范围

### 必做

1. 版本化、严格校验、纯 JSON 的 RunState 和 ToolCallState。
2. 本地 RunStore：路径 confinement、原子双槽保存、当前损坏回退上一有效版本、完成记录保留上限。
3. 模型调用前、模型完整响应后、授权提示前、Tool.run 前、每个工具结果后、终止时写 checkpoint。
4. 稳定工具调用 ID；空 ID/重复 ID 由运行时生成确定性替代 ID。
5. `planned -> awaiting_approval -> started -> completed/failed/skipped` 状态机，非法转换拒绝。
6. 恢复未开始的 planned 调用；已 completed/failed/skipped 调用不重放。
7. `started` 无结果时：被权限契约证明只读的调用可自动重试；其他调用必须人工选择 retry/skip/abort，
   非交互环境保持 paused，绝不默认重放。
8. 保存/恢复 ToolBudget、iteration/budget 扩展、重复签名计数、精确权限 grants、Conversation 与摘要
   checkpoint。
9. 新增 `assistant-agent runs` 与 `assistant-agent resume <run-id>`；chat 每轮 Run 关联 Session，完成后
   幂等同步 Session；`chat --resume` 对关联的未完成 Run 给出明确提示。
10. 定义指纹：provider/model、system prompt hash、tools schema hash。恢复不一致时交互确认；
    非交互拒绝。工具参数仍由当前 Registry schema 重新校验。
11. trace_id/session_id/run_id 分离并写入日志；新增 run_start/run_resume/run_checkpoint/run_end 事件，
    tool_call 带 run_id、provider/model；`/clear` 后绑定新 session，完成 D8。
12. 新增 crash/resume 单测和 deterministic eval；更新配置示例、README、ROADMAP、技术债和状态文档。

### 可选

- ToolContext 暴露当前稳定 call ID，支持未来工具把它作为外部 API idempotency key；内置工具本期不伪造
  外部 exactly-once 保证。
- `/runs` 作为 `runs` 命令的 chat 快捷入口；若会显著扩大 Slash 状态耦合，可只交付顶层 CLI。
- 完成/失败 Run 默认保留最近 100 个；非 terminal Run 永不自动清理。

### 不做

- 不引入 LangGraph、Temporal、数据库、消息队列或后台 worker。
- 不实现 time travel、任意 checkpoint 分叉、rewind 或从历史步骤改写后重跑。
- 不自动重放通用 Shell、网络、MCP、Skill 加载、用户交互或文件写入。
- 不承诺进程被 `kill -9` 后外部副作用 exactly-once；只能把不确定性显式化。
- 不实现跨机器恢复、checkpoint 加密、签名或不可信文件导入。
- 不异步化 AgentLoop，不解决进程树取消；留给 M10c 决策。
- 不修改 provider 抽象；恢复仍通过现有 `llm/client.py`。

## 5. RunState 数据契约

建议放在 `agent/run_state.py`，使用 Pydantic 严格模型：

```python
RunStatus = Literal["running", "paused", "completed", "failed"]
RunPhase = Literal[
    "model_pending", "tools_pending", "awaiting_approval",
    "tool_uncertain", "terminal",
]
ToolCallStatus = Literal[
    "planned", "awaiting_approval", "started", "completed", "failed", "skipped",
]

class ToolCallState(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]
    status: ToolCallStatus
    replay_policy: Literal["safe_readonly", "requires_decision"]
    permission_requests: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None  # code/is_error/executed + 模型可见 output

class RunState(BaseModel):
    schema_version: Literal[1]
    run_id: str
    session_id: str | None
    task: str
    status: RunStatus
    phase: RunPhase
    interactive: bool
    provider: str
    model: str
    system_prompt_hash: str
    tool_schema_hash: str
    messages: list[dict[str, Any]]
    compaction_checkpoint: dict[str, Any] | None
    iteration: int
    iteration_budget: int
    tool_budget: dict[str, int]
    last_signature: str | None
    repeat_count: int
    tool_calls: list[ToolCallState]
    permission_grants: list[dict[str, str]]
    terminal_text: str = ""
    session_synced: bool = False
    created_at: str
    updated_at: str
```

约束：所有字段必须 JSON 可序列化；加载时校验 assistant tool_calls 与 tool 结果 ID 完整配对；
`started` 不得带已确认结果；`completed/failed` Run 必须处于 terminal phase 且无 planned/started 调用；
进程中断或人工 abort 统一保存为可恢复的 `paused`，不另造含义重叠的 interrupted 状态。未来 schema
version 由显式 migration registry 升级，未知未来版本拒绝，不能“尽力猜”。Checkpoint 不保存 API key、
LLMClient、回调、logger、MCP session、Artifact 内容或任意 Python 对象。

## 6. 状态转换与持久化边界

一次正常工具轮次：

```text
model_pending --(模型完整响应持久化)--> tools_pending / terminal
tools_pending --(需要询问，先持久化)--> awaiting_approval
planned/awaiting_approval --(获准，先持久化)--> started
started --(ToolResult 已取得并写入对话，持久化)--> completed/failed
all calls resolved --> model_pending
```

具体边界：

1. 新任务把 user message、初始预算和 `model_pending` 原子保存，再调用 provider。
2. 流式模型正文可以只在流结束或中断时保存；模型返回完整 tool_calls 后，先规范 call ID、写 assistant
   tool_calls、保存整个 planned 批次，再执行任何工具。
3. Registry 取得 PermissionRequest 后，如策略需要询问，必须先保存 `awaiting_approval` 和请求明细，再调用
   Console。自动 allow/deny 也记录最终决定，但不制造虚假 pending。
4. 权限允许后、进入 `Tool.run` 前，先保存 `started`。该写入失败则 fail-closed，工具不执行。
5. Tool.run 返回后，把 ToolResult 和 tool message 一起保存为 completed/failed，再向 UI yield 结果。
   若此保存失败，停止运行；磁盘仍是 started，因此恢复时会正确进入不确定处理。
6. 工具批次全部有结果后保存 `model_pending`；预算耗尽、熔断、中断和错误保存 terminal/paused 状态。
7. terminal completed 后先幂等同步 Session，再把 `session_synced=true`；任一步崩溃都可在 resume 时补做，
   不会再次调用模型或工具。

Checkpoint 写入不是普通可观测日志。写失败时不得吞异常：副作用前停止且不执行；副作用后停止并保留
uncertain 语义。EventLogger 仍保持尽力而为，不反过来成为正确性依赖。

## 7. 恢复语义

### model_pending

此前没有未确认副作用，可重新调用模型。LLM 请求本身可能计费两次，但不会重放工具；这是明确接受的
at-least-once 模型调用语义。

### planned / awaiting_approval

工具尚未进入 Tool.run。恢复后重新做当前 Registry schema 校验与权限决策；已保存的 `always` 精确授权
恢复到本 Run 的 ToolContext。旧 pending approval 重新展示，不把“曾经显示过”当成已批准。

### started

- `safe_readonly`：仅限权限请求全部属于 workspace 文件读取，或带 `trusted_readonly=true` 的内置只读
  进程，并且不是 MCP/Skill/user interaction；可自动重试，日志标记 recovered_retry。
- `requires_decision`：展示工具名、脱敏参数、权限目标、call ID 和风险，让用户选择：
  - retry：用户明确承担可能重复执行的风险，复用同一 call ID；
  - skip：不执行，注入稳定 `recovery_skipped` tool result，允许模型重新评估；
  - abort：保持 paused/uncertain，不修改对话，不继续后续批次。
- 非交互环境只报告 run ID 与恢复命令，保持 paused；不能把默认 deny 等同于“工具没执行过”。

### 定义不一致

provider/model、system prompt 或 tools schema 指纹变化时，恢复前显示差异。交互确认后可用当前定义继续；
非交互拒绝。不存在的工具不执行；planned 调用可转稳定错误结果，started 调用仍按 uncertain 处理。

## 8. RunStore

新增 `session/run_store.py`，默认目录 `./.assistant_agent/runs/`：

- `<run_id>.json` 为当前槽，`<run_id>.prev.json` 为上一有效槽；保存时先把 current 原子轮换为 prev，
  再把 fsync 完成的同目录 temp 原子替换为 current。任意时刻至少保留一个可读槽。
- load 依次校验 current、prev；current 损坏时回退 prev 并返回 recovery warning；两者都坏才失败。
- run ID 使用现有安全字符规则并做 resolve confinement；不接受用户指定文件路径。
- RunStore 只接收/返回 JSON object（`dict[str, Any]`）及槽位回退元数据，不 import `agent`；
  `RunCoordinator` 在保存前执行 `RunState.model_validate()`，加载后再次校验并重建严格模型。这样保持
  `agent(3) -> session(0)` 的单向依赖，同时仍只使用 JSON/Pydantic，不反序列化任意类实例。
- `list()` 返回状态、phase、session、更新时间和任务预览；`delete()` 对非 terminal Run 要求确认。
- prune 只删除最旧 terminal 且 session_synced 的记录；`running/paused`（包括 `tool_uncertain` phase）
  永不自动删除。
- 默认配置建议：`agent.recovery.enabled=true`、`max_completed_runs=100`。关闭时 Loop 无 coordinator，
  现有行为保持；README 明确关闭意味着崩溃后不能恢复。

## 9. 内核与 Registry 接口

新增 `agent/recovery.py`：

- `RunCoordinator` 持 RunState 与 RunStore，提供语义方法：`before_model`、`model_completed`、
  `approval_pending`、`tool_started`、`tool_completed`、`terminal`、`restore`。
- AgentLoop 只调用这些方法并恢复必要计数，不直接读写路径/JSON；coordinator 为 None 时路径与现状一致。
- 从 `_run_task` 抽出小的“执行已计划工具批次”和“恢复游标”帮助函数，避免 loop.py 因 M10b 逼近/超过
  500 行硬线。拆分依据是可恢复状态机职责，不是机械追 300 行。

新增 `tools/lifecycle.py`：

- `ToolExecutionLifecycle` 协议接收 call ID、权限请求和 ToolResult。
- `ToolRegistry.execute(..., call_id="", lifecycle=None)` 保持旧调用兼容。
- Registry 在授权提示前、Tool.run 前和结果后调用 lifecycle；checkpoint hook 异常不被 tool_exception
  兜底吞掉。pre/post security observers 的 fail-closed/观察语义保持不变。
- replay_policy 由权限能力保守计算；工具声明不足时默认 `requires_decision`。
- 协议定义留在 `tools/lifecycle.py`，`agent` 侧 coordinator 实现该协议；`tools` 只面向协议调用，绝不
  import `agent`。架构测试继续强制 `tools -> agent/ui` 为零依赖。

这是 M10b 必须修改 `agent/loop.py` 的理由：iteration、重复签名、模型响应与 Conversation 写回都由 Loop
拥有，外部 observer 无法在不破坏协议的情况下重建这些状态。改动应是“显式状态移交”，不是重写 ReAct。

## 10. CLI 与会话

- `assistant-agent runs`：列出可恢复 Run；支持 `--delete <id>`，未完成状态删除前确认。
- `assistant-agent resume <run-id>`：加载状态、构建当前 Runtime、执行定义兼容检查，再从游标继续。
- 新 run/chat 轮次启动时显示 run ID，并写 run_start；故障提示始终包含恢复命令。
- chat 每轮 Run 绑定当前 Session ID；完成后 coordinator/CLI 幂等保存 history 与 compaction checkpoint。
- `chat --resume <session-id>` 保持原语义；若发现该 Session 有未完成 Run，提示使用 resume，不静默从旧
  Session 尾部另开任务。没有 Run checkpoint 时完全按旧 Session 恢复。
- `/clear` 创建 Session 后重新绑定 logger；当前 Run 未 terminal 时不允许清空，正常聊天输入点不存在
  `running/paused` Run。
- resume 默认使用记录的 provider（若当前配置仍存在）；显式 `--provider` 可覆盖并触发兼容确认。

## 11. 日志标识

- `trace_id`：一次 CLI 进程/Runtime 生命周期。
- `session_id`：可选的聊天 Session；run 单次模式为空，不再拿随机 trace 冒充。
- `run_id`：一次用户任务，跨进程 resume 保持不变。
- EventLogger 提供 bind_session/start_run/end_run；每条 run 内事件带三种 ID 中适用的字段。
- tool_call 记录 run_id、call_id、provider/model；run_resume 记录来源 phase、槽位回退和定义差异。
- `/model` 现有 model_switch 保留；后续 tool_call 使用新模型。`/clear` 后 session_id 立即变化。

这会还清 D8；历史 JSONL 字段继续可读，但聚合代码应容忍旧记录没有 trace_id/run_id。

## 12. 文件与依赖调整

- `agent/run_state.py`：严格状态模型、ID 规范化、版本迁移、定义指纹。
- `agent/recovery.py`：RunCoordinator、状态转换与恢复决策。
- `session/run_store.py`：不感知 RunState 类型的双槽原子 JSON 存储、列表/清理/回退。
- `tools/lifecycle.py`、`tools/registry.py`、`tools/base.py`：工具生命周期与审批前 checkpoint hook。
- `agent/loop.py`：模型/工具边界接入 coordinator，导入/导出运行计数，恢复入口。
- `cli/setup.py`：装配 RunStore/coordinator 所需工厂，不让 Runtime 固定绑定某个 run。
- `main.py`：runs/resume 命令、chat Session 同步与未完成运行提示。
- `obs/logger.py`：trace/session/run 标识与恢复事件。
- `config/schema.py`、`config.example.yaml`：recovery 配置。
- `evals/`：crash fixture/结果断言；README/ROADMAP/TECH_DEBT/状态文档同步。

不新增第三方运行时依赖；Pydantic、JSON 和现有原子 I/O 足够。

## 13. 实施顺序

1. P1 状态与存储：RunState、状态转换、双槽 RunStore、版本/路径/损坏回退测试。
2. P2 工具边界：Registry lifecycle、审批前/副作用前/结果后 checkpoint、稳定 call ID、fail-closed 测试。
3. P3 Loop：模型轮次、预算、重复签名、planned batch 和 terminal 接入；默认无 coordinator 回归。
4. P4 恢复：planned/safe-readonly/uncertain 三路径、retry/skip/abort、定义指纹检查。
5. P5 CLI/Session/日志：runs/resume、chat 关联与幂等同步、trace/session/run ID 对齐，还 D8。
6. P6 eval/文档：crash/resume deterministic case、全量 DoD、技术债与状态同步、归档提交。

每批先跑聚焦 pytest、Ruff 和 mypy；不得等全部改完才处理工具协议或 Session 回归。

## 14. 测试计划

- State：合法/非法转换、稳定 fallback call ID、重复 call ID、未知 future version、migration registry。
- Architecture：`session/run_store.py` 不依赖 agent，`tools/lifecycle.py` 不依赖 agent/ui，Loop 不依赖
  session 的文件路径或 UI；现有分层适应度测试继续全绿。
- Store：current/prev 往返、替换前故障、current 损坏回退 prev、双损坏、路径逃逸、terminal pruning、
  surrogate/非 JSON 值拒绝。
- Model crash：before_model 后崩溃可重新调用；完整 tool plan 保存后崩溃不再调用模型。
- Tool crash：planned 时崩溃可执行；started 前故障工具未运行；副作用后/结果保存前故障恢复为 uncertain；
  completed 第一调用后崩溃只执行批次剩余调用。
- Approval：confirm 回调模拟进程退出，磁盘已有 awaiting_approval；恢复后重新展示；always grant 在同 Run
  保留但不跨 Run；非交互不执行 uncertain。
- Recovery choice：retry 复用 call ID、skip 注入配对 tool result、abort 不改对话；只读自动重试。
- Budget/fuse：used_calls/used_output、iteration extension、last_signature/repeat_count 恢复后不重置绕过上限。
- Context：compaction checkpoint 与 raw history 一致；恢复后 tool_call/tool_result 协议完整。
- Compatibility：provider/prompt/tool hash 相同直续，不同需确认；工具消失/参数 schema 改变不执行旧参数。
- Session：旧 JSON 载入、无 RunState 普通 chat、terminal 后幂等同步、同步窗口崩溃不重跑。
- Logging：trace/session/run/call ID；`/clear` 与 `/model` 后字段正确；旧日志消费者容忍新增字段。
- CLI：runs 列表、resume 成功、损坏回退警告、unknown ID、非 TTY uncertain 退出码。
- Eval：至少 4 个 deterministic case：planned 批次恢复、部分批次不重放、uncertain 副作用暂停、
  预算/重复状态恢复。
- 全量：pytest/coverage、Ruff format/check、mypy、架构测试、scripted eval。

故障注入必须发生在真实持久化边界，不能只手工构造“看起来像崩溃后”的最终 JSON。测试可用专用
`SimulatedCrash(BaseException)`，确保不会被普通 Exception 兜底误吞。

## 15. 验收标准

1. 模型返回包含两个工具的批次后崩溃，恢复不会再次调用模型；已完成第一个工具不重放，只处理第二个。
2. 写文件/Shell/MCP 在 started 后进程退出，恢复默认不执行；非交互稳定暂停，交互必须明确选择。
3. 权限提示出现前磁盘已有 pending approval；重启后请求可重现且未被误记为批准。
4. 工具结果已 checkpoint 后再崩溃，恢复不重复工具，tool_call/tool_result 配对仍完整。
5. 预算、最大轮数和重复熔断不能通过重启清零；compaction checkpoint 不重复摘要。
6. current checkpoint 损坏时自动使用 prev 并告警；两个槽都坏时安全失败，不猜测状态。
7. Run 完成但 Session 同步窗口崩溃时，resume 只补 Session，不调用模型/工具。
8. 旧 Session、恢复关闭配置、无 coordinator 的 AgentLoop 行为不回退。
9. 日志可按 trace/session/run/call/provider/model 聚合，D8 标记还清。
10. 全量 DoD 通过；计划归档；状态数字来自实测；无 checkpoint、密钥或本地产物入库。

## 16. 风险与控制

- **伪 exactly-once**：文档和 UI 统一使用“已确认/不确定”，不把 started 当成功，也不把 checkpoint 当
  外部事务日志。retry 必须由用户显式承担风险。
- **checkpoint 自身写失败**：副作用前 fail-closed；副作用后保留 started 并停止。不得按日志失败处理。
- **频繁 fsync 性能**：只在模型/审批/工具/终止边界写，不按 token delta 写；用实测评估，不预先降级
  正确性。若性能不足再做批量 journal，不先引入数据库。
- **Loop 膨胀**：状态模型和恢复策略放新模块；Loop 只保留顺序编排。若仍逼近 500 行，抽工具批次执行器，
  不放宽架构硬线。
- **Checkpoint 泄密**：RunState 会包含对话和工具参数，目录随 `.assistant_agent/` gitignore；README 明示
  本地敏感数据边界。日志脱敏不等于 checkpoint 脱敏，不能误导用户。
- **定义漂移**：保存 prompt/tool/provider 指纹；长期挂起恢复必须确认变化，未知 schema version 拒绝。
- **权限授权扩大**：只持久化精确 PermissionScope，生命周期限同一 run；不跨 run/session 自动继承。
- **Session/Run 双写**：非 terminal Run 为恢复权威，Session 只在 terminal 后幂等同步；用 session_synced 标志
  处理崩溃窗口。
- **范围膨胀**：不做异步、并行、进程树取消、time travel 或跨机恢复；这些不应借 M10b 混入。
