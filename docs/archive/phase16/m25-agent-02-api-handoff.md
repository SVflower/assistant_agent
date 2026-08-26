# M25-AGENT-02 Agent -> API 交接

## API 必改

1. paused Run 的 worker 已退出后，调用对应 `SessionRuntime.cancel_run(run_id)`；消费返回的
   `RunExecution.events`。首次状态转换产生唯一 `run_terminal(cancelled)`，重复调用返回空事件流。
2. active Run 继续调用 `SessionRuntime.cancel()`，并由原 worker 持续消费直到 Agent 发布 terminal；不得
   同时调用 `cancel_run()` 创建第二个消费者。
3. Agent 的 Run Iterator 发生未分类 `Exception` 时会自行保存
   `failed/terminal + RunFailure(code=internal_error)`、同步 Session 并发布唯一真实 terminal。API 删除
   synthetic `run.terminal(failed/internal_error)` 及对应 checkpoint 状态机。
4. API 只在收到 Agent `run_terminal` 后更新网络 Run snapshot。Iterator 自身异常表示传输/基础设施失败，
   可重新读取 Agent Run snapshot，但不得猜测或改写 Agent 终态。
5. `SessionBusyError` 表示目标仍由 worker 管理；`SessionRunConflictError` 表示归属错误或已有
   completed/failed 终态，均不得转成 force-delete。

## 兼容影响

- `EVENT_CONTRACT_VERSION == 1`，checkpoint schema v4，ItemEvent 字段与 terminal 顺序不变。
- `SessionRuntime.cancel_run(run_id) -> RunExecution` 是 additive 公共服务能力。
- CLI 行为不变；API/Web 不需要导入 Agent 内部模块。

## 联调序列

```text
paused checkpoint -> worker exit -> cancel_run(run_id)
-> checkpoint cancelled/terminal
-> Session sync + session_synced=true
-> run_terminal(cancelled) exactly once
-> delete_session allowed
```

```text
Agent event source raises Exception
-> checkpoint failed/terminal(internal_error)
-> Session sync + session_synced=true
-> run_terminal(failed, structured failure) exactly once
```

API 契约测试至少覆盖恢复后取消、重复取消、active 冲突、错误 Session 归属、异常终态不合成，以及终态后
Session 删除。
