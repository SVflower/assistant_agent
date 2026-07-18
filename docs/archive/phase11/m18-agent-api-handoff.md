# M18 Agent API 正式交接

## 适用范围

本文供 `assistant_agent_api` 及后续任何进程内调用方接入 M18。调用方继续通过安装后的
`assistant-agent` Python 包使用 `AgentService` / `SessionRuntime`，不得解析 CLI 输出、日志、
Python 异常或中文错误文本。

M18 不要求 API 改成 async。API 可继续在工作线程迭代同步 `Iterator[StepEvent]`，自行增加 seq、
timestamp、session_id、run_id、heartbeat、重连缓存和 WebSocket DTO。

## 版本

- `StepEvent.contract_version`：仍为 `1`。本期仅新增可选字段和 `activity` kind，既有字段含义未变。
- Run checkpoint `schema_version`：`2 -> 3`。
- API 若维护独立 Web contract，可按自身兼容策略升版；不得把 Agent checkpoint 版本当作 Web 版本。

## StepEvent

公共导入：

```python
from assistant_agent.service import BudgetSnapshot, RunFailure, StepEvent
```

完整字段：

```text
kind: reasoning | content_delta | tool_call | tool_result | usage | final |
      error | interrupted | notice | activity | run_terminal
text: str
tool_name: str
tool_args: dict | null
is_error: bool
usage: dict[str, int] | null
call_id: str
display: ToolDisplay | null
result_code: str
result_metadata: dict | null
contract_version: int
sensitive: bool
terminal_status: completed | failed | paused | cancelled | null
failure: RunFailure | null
phase: ActivityPhase | null
budget: BudgetSnapshot | null
```

API 规则：

- `reasoning` 始终 `sensitive=true`，不得发送到普通 Web 客户端或持久化为用户可见消息。
- 工具展示优先使用 `display`；不得向浏览器原样发送 `tool_args` 或敏感 metadata。
- `failure` 是行为判断来源，`text` 只用于展示。
- 未识别的新 kind/字段必须按向后兼容规则忽略，不能使 Run 消费失败。

## RunFailure

```text
code: FailureCode
safe_message: str
retryable: bool
allowed_actions: AllowedAction[]
resource: iterations | tool_calls | tool_output | context | null
used: int | null
limit: int | null
terminal_status: failed | paused | null
phase: ActivityPhase
unknown_side_effect: bool
```

`safe_message` 可直接面向用户，但 API/Web 不得根据其内容决定按钮或状态。普通可纠正的
`tool_result.failure` 使用 `terminal_status=null`，表示 Run 尚未因此进入终态。

### FailureCode

| code | 含义 |
|---|---|
| `tool_output_budget_exhausted` | 当前 Run 累计工具输出字符预算耗尽 |
| `tool_call_budget_exhausted` | 当前 Run 工具调用次数预算耗尽 |
| `iteration_limit_reached` | 当前 Run 模型迭代预算耗尽 |
| `context_limit_exceeded` | 请求无法装入配置/模型上下文窗口 |
| `provider_rate_limited` | Provider 返回限流，通常可稍后重试 |
| `provider_unavailable` | Provider 连接失败或临时 5xx |
| `provider_timeout` | Provider 请求超时 |
| `tool_failed` | 工具执行失败；查看 retryable/unknown_side_effect |
| `permission_denied` | 工具权限拒绝或权限检查安全失败 |
| `dependency_unavailable` | MCP 等外部工具依赖不可用或契约失败 |
| `internal_error` | 配置、永久 Provider 错误或 Agent 内部失败的安全归类 |

### AllowedAction

合法值：

- `continue`：仅在当前 continuation Interaction 尚未决时合法。
- `stop`：拒绝/停止等待，安全默认。
- `resume_run`：恢复 paused Run。
- `retry_run`：重新运行任务；不是自动重放未知副作用工具。
- `start_new_run`：基于用户调整后的输入启动新 Run。
- `adjust_configuration`：调整模型窗口、预算或 Provider 配置后再启动。
- `inspect_dependency`：检查 MCP/Provider 等依赖状态。
- `resolve_uncertain_tool`：对未知副作用提交 retry/skip/abort recovery 决策。

Web 只能展示 `allowed_actions` 中的动作。`retryable=true` 不等于允许自动重试，尤其当
`unknown_side_effect=true` 时只能走 recovery Interaction。

## BudgetSnapshot

```text
iterations_used / iterations_limit
tool_calls_used / tool_calls_limit
tool_output_chars_used / tool_output_chars_limit
```

这些字段是安全数值，可进入 Run snapshot。`tool_output_chars_limit=0` 表示未启用累计输出限制。

## ActivityPhase

- `preparing_context`
- `calling_model`
- `executing_tool`
- `waiting_interaction`
- `saving_checkpoint`
- `syncing_session`

activity 是运行事实，不是 reasoning 摘要。API 可映射为短状态文案和 spinner，但不得把它当 assistant
消息写入 Session history。

## Continuation Interaction

Agent 继续复用公共 `InteractionPort.confirm_continue()`。API 使用 `BlockingInteractionPort` 时，从
`next_request()` 得到：

```text
ContinueRequest
  request_id, run_id, session_id, call_id
  kind = "continue"
  reason: iteration_limit_reached | tool_call_budget_exhausted |
          tool_output_budget_exhausted
  resource: iterations | tool_calls | tool_output
  used, limit
  suggested_increment
  hard_limit
  extension_count, max_extensions
  legal_options = [continue, stop]
  iterations_used, iteration_limit  # iteration 旧字段，兼容保留
```

响应：

```text
ContinueDecision
  request_id: 必须精确匹配
  continue_run: bool
```

约束：

1. 默认 `stop`；超时、断线、Runtime close、异常、错误/重复 request_id 均不能继续。
2. Web 只允许提交 `legal_options` 中的决定。
3. continue 只增加当前 Run，不修改服务端全局配置。
4. Agent 在继续执行前把新 limit、扩展次数和 decision checkpoint。
5. 达到 hard_limit 或 max_extensions 后 Agent 不再发布可继续请求，终止失败中也不会包含
   `continue` action。

## checkpoint 兼容

RunState v3 新增：

```text
failure
iteration_continuation
tool_call_continuation
tool_output_continuation
continuation_decisions[]
```

Agent 自动迁移 v1/v2：保留当时的当前预算作为初始 limit，扩展次数设为 0；旧 failed Run 因没有
可靠分类，迁移为 `internal_error`，不会伪造具体失败原因。未知未来版本继续拒绝。

恢复以 checkpoint 中的 limit、extension_count 和 request_id 为准。同一 request_id 的成功响应幂等，
不会重复扩展；已经 completed/failed/skipped 的 call 仍按 call_id 跳过，不重放。

## final 与终态

固定规则：

1. `final` 只表示完整 assistant 最终正文。
2. `final` 不改变 Run 状态，也不等于完成。
3. `completed/failed/paused/cancelled` 只通过 `run_terminal` 表达。
4. 每个正常耗尽的事件迭代器恰好一个 `run_terminal`。
5. failed 的 `run_terminal.failure` 必须非空。
6. Provider 中途失败前的 `content_delta` 可保留为 partial UI 内容，但 Agent 不发送伪 `final`。
7. API 只有收到 `run_terminal` 后才能写最终 Run snapshot；`final` 可先作为候选正文缓存。

## 推荐 WebSocket 映射

API 可按如下规则映射，不改变 Agent 语义：

```text
StepEvent.activity      -> run.activity（覆盖临时状态）
content_delta           -> assistant.delta（partial=true）
tool_call/tool_result   -> run.tool（使用 call_id 配对，优先 display）
usage                   -> run.usage
final                   -> assistant.final_candidate
error/interrupted       -> run.notice（不是终态）
run_terminal            -> run.terminal + 原子更新 Run snapshot
InteractionRequest      -> interaction.request
InteractionDecision     -> interaction.response ack
```

API 应在自己的事件封套增加 `seq/timestamp/session_id/run_id`。网络重连只重放 API 缓存的 DTO，不能
重新迭代 Agent 或重新提交 InteractionDecision。

## 示例序列

### 正常完成

```json
{"kind":"activity","phase":"preparing_context"}
{"kind":"activity","phase":"calling_model"}
{"kind":"content_delta","text":"完成"}
{"kind":"final","text":"完成"}
{"kind":"activity","phase":"syncing_session"}
{"kind":"run_terminal","terminal_status":"completed","failure":null}
```

### 工具输出预算停止

```json
{"kind":"tool_result","call_id":"call-1","result_code":"ok"}
{"kind":"activity","phase":"waiting_interaction"}
{"interaction":{"kind":"continue","reason":"tool_output_budget_exhausted","resource":"tool_output","used":30000,"limit":30000,"legal_options":["continue","stop"]}}
{"decision":{"request_id":"interaction-...","continue_run":false}}
{"kind":"error","failure":{"code":"tool_output_budget_exhausted","resource":"tool_output","used":30000,"limit":30000,"retryable":true,"allowed_actions":["retry_run","adjust_configuration","start_new_run"]}}
{"kind":"activity","phase":"syncing_session"}
{"kind":"run_terminal","terminal_status":"failed","failure":{"code":"tool_output_budget_exhausted"}}
```

### 工具调用预算继续

```json
{"kind":"activity","phase":"waiting_interaction"}
{"interaction":{"kind":"continue","reason":"tool_call_budget_exhausted","resource":"tool_calls","used":50,"limit":50,"suggested_increment":20,"hard_limit":200,"extension_count":0,"max_extensions":2}}
{"decision":{"request_id":"interaction-...","continue_run":true}}
{"kind":"activity","phase":"executing_tool","tool_name":"read_file"}
{"kind":"tool_call","call_id":"call-2","tool_name":"read_file"}
{"kind":"tool_result","call_id":"call-2","result_code":"ok"}
```

### Provider 429

```json
{"kind":"activity","phase":"calling_model"}
{"kind":"error","failure":{"code":"provider_rate_limited","safe_message":"模型服务请求过于频繁，请稍后重试。","retryable":true,"allowed_actions":["retry_run","stop"]}}
{"kind":"activity","phase":"syncing_session"}
{"kind":"run_terminal","terminal_status":"failed","failure":{"code":"provider_rate_limited"}}
```

### 未知工具副作用

```json
{"kind":"tool_result","call_id":"call-9","result_code":"mcp_outcome_unknown","failure":{"code":"tool_failed","unknown_side_effect":true,"terminal_status":"paused","allowed_actions":["resolve_uncertain_tool","stop"]}}
{"kind":"interrupted","text":"工具执行结果未知，Run 已暂停，等待恢复决策。"}
{"kind":"run_terminal","terminal_status":"paused","failure":{"code":"tool_failed","unknown_side_effect":true,"terminal_status":"paused"}}
```

调用 `resume_run(run_id)` 后，Agent 发布既有 `RecoveryRequest`，API 必须让用户明确选择
`retry/skip/abort`。retry 可能重复副作用，绝不能因为网络重连自动选择。

## API 最小改造清单

1. WebSocket/Run snapshot 增加 `failure`、`phase`、`budget` 可选字段。
2. 枚举化 failure code 和 allowed action，不解析 message。
3. Interaction broker 支持扩展后的 ContinueRequest 字段，响应继续使用精确 request_id。
4. Run snapshot 只由 `run_terminal` 收口；failed 保存完整脱敏 failure。
5. reasoning 继续过滤；tool 参数继续使用 ToolDisplay 脱敏摘要。
6. 接受新增 activity kind 和未知未来字段，保持前向兼容。
7. 加入上述四类端到端事件序列测试，尤其验证 `run_terminal` 恰好一次。
