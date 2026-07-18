# Assistant Agent 服务调用指南

> 适用对象：需要在 Python 进程内调用 `assistant_agent` 的 Web API、桌面应用、后台任务、自动化平台
> 或其他上层服务。
>
> 本文是公共服务契约的长期唯一正式入口；里程碑归档和阶段性交接不能替代本文。
> 当前公共事件契约：`EVENT_CONTRACT_VERSION == 1`；当前 Run checkpoint：schema v3。
> 最近同步：M18，Agent commit `0ef635ab5b14cec4b4e7ffa844956a1811881e99`。

## 1. 集成边界

`assistant_agent` 提供同步、UI 无关的 Python 服务接口。调用方不需要启动 CLI 子进程，也不应解析
Rich 终端输出。

```text
上层服务
  -> assistant_agent.service       Runtime、Session、Run、恢复和事件
  -> assistant_agent.interaction   授权、澄清和恢复决策
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
from assistant_agent.interaction import ...
from assistant_agent.service import ...
```

不要导入：

```python
assistant_agent.cli
assistant_agent.ui
assistant_agent.agent.loop
assistant_agent.cli.recovery
```

这些模块不是服务集成协议，直接依赖会绕过公共生命周期或造成升级耦合。

## 3. 推荐入口

业务服务优先使用 `AgentService`。它收编了 Session、Run、恢复和终态同步，不要求调用方复制状态机。

```python
from pathlib import Path

from assistant_agent.service import AgentService, RuntimePolicy

policy = RuntimePolicy(
    allow_extension_management=False,
    allow_personal_skills=False,
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
禁止的扩展管理、personal Skill 或 MCP transport，也不能把 sandbox 降到 policy 下限以下。CLI 使用
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
run_list = service.list_runs(session_id=session_id)
unfinished = session.unfinished_runs()
```

一个 `SessionRuntime` 独占以下状态：

- Conversation/history 和 compaction checkpoint；
- RunControl；
- 会话级权限记忆；
- InteractionPort；
- MCPManager、WebClient、Workspace 和日志资源。

同一 Session 不得创建多个 `SessionRuntime` 并并行运行。调用方应建立以 `session_id` 为键的 Runtime
Registry，并对加载和淘汰操作加锁。

### 5.2 删除

```python
deleted = service.delete_session(session_id)
```

Session 存在 running/paused Run 时，默认抛出 `SessionRunConflictError`。服务不应为方便删除而隐式
取消或丢弃可恢复 Run。`force=True` 只适合已由产品策略明确确认的数据清理流程。

### 5.3 关闭

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

for event in execution.events:
    publish(event)
```

`execution.events` 是同步、惰性 `Iterator[StepEvent]`：

- 创建 `RunExecution` 不等于任务已经执行完；
- 必须在工作线程中持续消费 Iterator；
- 不要先 `list(events)` 再向客户端一次性发送，这会失去流式能力；
- 正常公共流最后有一个 `kind == "run_terminal"`；
- 调用方提前关闭或放弃 Iterator 时，Agent 会尝试把 Run 安全暂停，而不是误记为 completed。

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

resumed = session.resume_run(run_id)
assert resumed.run_id == run_id
for event in resumed.events:
    publish(event)
```

- pause：保存可恢复状态；
- cancel：进入不可继续的 cancelled 终态；
- resume：沿用原 `run_id`，校验 provider/model/system prompt/tool schema 变化；
- 定义变化未经 InteractionPort 接受时保持 paused；
- `tool_uncertain` 必须由用户选择 retry/skip/abort，不能默认重放可能有副作用的工具。

Run 达到 completed/failed/cancelled 后，公共门面会先同步 Session，再设置 `session_synced=True`。同步
失败时保留未同步 checkpoint，调用方不要自行伪造成功状态。

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
terminal_status, failure, phase, budget
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
- shutdown 时先 `session.close()`，再等待 worker。

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

## 10. Runtime notice 与异常

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

快照包含 sandbox、工具名、Skill 指纹和 MCP server 的
`connected/degraded/disabled/blocked/required_failed` 状态，不包含 header、env、完整命令、原始异常、
工具原始 Schema 或 Skill 正文。调用方应映射这些字段，不要解析 notice 文本推断能力。

公共异常：

| 异常 | 含义 | 常见服务映射 |
|---|---|---|
| `RuntimeConfigError` | 配置无效 | 启动失败或 503 |
| `RuntimeInitializationError` | Runtime 某阶段启动失败 | 503，记录 `stage` |
| `RuntimePolicyError` | config 试图突破部署 policy | 部署错误或 503 |
| `RuntimeDependencyError` | required MCP 不可用 | 503 capability_unavailable |
| `RuntimeClosedError` | Runtime 已关闭 | 409/410 |
| `SessionBusyError` | Session 已有活跃 Run | 409 |
| `SessionRunConflictError` | 未完成 Run 冲突或归属错误 | 409 |

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
- active/paused Runtime 不热插拔工具；能力恢复后只重建无未完成 Run 的空闲 Runtime；
- Runtime 重建使用原 session_id 调用 `load_session()`，并安全清空内存授权记忆。

状态默认写入：

```text
%USERPROFILE%/.assistant_agent/workspaces/<workspace-id>/
  sessions/
  runs/
  logs/
  artifacts/
  mcp-stderr/
```

可通过 `ASSISTANT_AGENT_HOME` 改变用户级状态根目录。多个服务实例若共享同一状态目录，还需要由上层
服务提供进程间所有权或租约；当前公共门面只保证单进程内的 SessionRuntime 约束。

## 12. Web API 参考映射

推荐把 REST 作为控制面，把 WebSocket/SSE 作为事件面：

```text
POST   /sessions
GET    /sessions/{session_id}
DELETE /sessions/{session_id}
POST   /sessions/{session_id}/runs
GET    /runs/{run_id}
POST   /runs/{run_id}/pause
POST   /runs/{run_id}/cancel
POST   /runs/{run_id}/resume
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
usage               -> run.usage
final               -> assistant.final_candidate
error/interrupted   -> run.notice（不是终态）
run_terminal        -> run.terminal + 原子更新 Run snapshot
```

Run snapshot 至少保留 `terminal_status/failure/current_phase/budget/pending_interaction/final_candidate`。
API 自行增加 seq、timestamp、session_id、run_id、heartbeat 和重连缓存；网络重连只能重放 API 已缓存
DTO，不能重新迭代 Agent 或重新执行工具。

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

## 14. 接入验收清单

1. 只导入 `assistant_agent.service` 和 `assistant_agent.interaction`；
2. 启动时验证 `EVENT_CONTRACT_VERSION`；
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

`assistant_agent_api` 的具体交接记录见
[archive/phase11/m18-agent-api-handoff.md](archive/phase11/m18-agent-api-handoff.md)；M16 初始边界记录见
[m16-assistant-agent-api-handoff.md](m16-assistant-agent-api-handoff.md)。这些文件是历史交接，发生冲突时
以本文和安装版本导出的公共 Python 类型为准。
