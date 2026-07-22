# 03 Agent Loop 与上下文

## ReAct 在这里是什么

一次典型任务不是只调用一次模型，而是重复以下步骤：

```text
用户任务 -> 打包上下文 -> 调用模型 -> 文本或工具请求
                                  | 工具请求
                                  v
                         校验/授权/执行工具
                                  |
                                  +---- 工具结果加入上下文 ----+
```

模型返回最终文本时结束；达到预算、暂停、取消或失败时也会结束。`agent/loop.py` 是总控，单轮模型处理
在 `agent/turn.py`，工具批次在 `agent/tool_batch.py`。拆分的目的不是缩短文件，而是让“模型一轮”和
“工具一批”的局部规则可单独测试。

## 一次 Run 的简化时序

```mermaid
sequenceDiagram
    participant Caller as CLI/API
    participant Loop as AgentLoop
    participant Coord as RunCoordinator
    participant Model as Provider
    participant Registry as ToolRegistry
    Caller->>Loop: run(task, coordinator)
    Loop->>Coord: before_model + checkpoint
    Loop->>Model: stream(messages, tools)
    Model-->>Loop: text chunks / tool calls / usage
    Loop-->>Caller: StepEvent stream
    Loop->>Coord: model_completed + checkpoint
    Loop->>Registry: execute(tool call)
    Registry->>Coord: approval_pending / tool_started / tool_completed
    Registry-->>Loop: ToolResult
    Loop->>Coord: terminal transition
    Loop-->>Caller: final (仅完整正文)
    Loop-->>Caller: run_terminal (唯一终态)
```

## final 不等于 completed

`final` 只表示“有一段完整 assistant 正文可以展示”。Run 的真实终态只由唯一的
`run_terminal` 表示，状态可能是 `completed`、`failed`、`paused` 或 `cancelled`。调用方不能看到
`final` 就自行把数据库 Run 标记完成。

失败前模型已经输出的零散文本可以保留为 partial，但不能伪装成 `final`。这保证 Web 不会把半句话当成
完整答案。

## StepEvent 是什么

`contracts/events.py:StepEvent` 是同步事件 DTO。常见 kind：

- `activity`：安全运行事实，如 `calling_model`、`executing_tool`。
- `content_delta`：可展示正文的流式片段。
- `reasoning`：敏感事件，明确标记 `sensitive=True`，服务端不应下发隐藏推理。
- `tool_call` / `tool_result`：通过稳定 `call_id` 配对，展示优先用脱敏 `ToolDisplay`。
- `usage`：模型安全用量。
- `final`：完整最终正文。
- `run_terminal`：唯一 Run 终态，可携带结构化 `RunFailure`。

API 可以为网络传输增加 seq、timestamp、run_id，但不能修改 Agent 事件本身的语义。

## 三种状态必须分清

### Conversation

`agent/context/conversation.py` 管模型上下文。它保存 raw history、压缩 checkpoint，并根据模型窗口构造
provider-facing messages。压缩是为了省 token，不应改写公开会话事实。

### Session

Session 是长期公开会话。schema v3 的 message ledger 给公开 user/assistant 消息稳定 ID、时间和
`reply_to_message_id`。它用于列表、恢复、导出和 fork。

### RunState

RunState 是一次任务的可恢复执行事实，包含 phase、iteration、预算、待执行工具、定义哈希、failure 和
Session 同步标志。它不是 UI loading state，也不是长期聊天历史。

例如上下文压缩后：Conversation 给模型的历史可能只剩摘要和最近消息，但 Session ledger 仍保留完整
公开消息；RunState 记录本次任务恢复所需的精确边界。

## 上下文预算

模型窗口不只包含历史文本，还包括 system prompt、工具 schemas 和预留输出。封套大致是：

```text
system prompt + tool schemas + history + reserved output <= max_context_tokens
```

超过软阈值时可生成 compaction summary；最终硬封套仍会拒绝不兼容请求。不能只按照“消息条数”判断，
因为一条工具 schema 或工具输出可能很大。

## 为什么 checkpoint 放在这些位置

危险工具的顺序必须是：

```text
参数已验证 -> 权限待确认 checkpoint -> 授权 -> started checkpoint -> 真实副作用 -> completed checkpoint
```

如果进程在副作用后、completed checkpoint 前崩溃，系统只能知道“可能执行过”，因此恢复进入
`tool_uncertain`，要求 retry/skip/abort，不能自动重放。

## 阅读建议

先看 `AgentLoop.run()` 和 `resume()` 的外层控制，再看 `RunCoordinator` 的状态方法。不要一开始钻进
prompt 文本。随后用 `tests` 中 Fake provider 案例跟踪 `StepEvent` 顺序。

