# 05 Session、Run 与恢复

## Session 是公开会话事实

Session schema v3 保存：会话 ID、标题、provider/model、公开 message ledger、压缩 checkpoint、图表
Artifact 引用和元数据版本。公开消息 ID 在迁移、重启、compaction、catalog 和 fork 后保持稳定。

assistant 消息通过 `reply_to_message_id` 指向对应 user 消息，而不是靠数组位置或文本猜测。这使并发、迁移
和 fork 边界可验证。

## Run 是一次执行事实

同一 Session 同时最多一个活跃 Run。RunState 的核心状态：

```text
status: running | paused | completed | failed | cancelled
phase: model_pending | tools_pending | awaiting_approval | tool_uncertain | terminal | ...
```

还保存 iteration、工具调用状态、预算、Conversation 快照、failure、presentation 和
`session_synced`。只有 `RunCoordinator` 应修改这些状态；其他模块调用它的语义方法。

## SessionRuntime 做什么

`application/runs.py:SessionRuntime` 把一个 `AgentRuntime` 与一个 Session 绑定，提供：

- `start_run(task)`：获得 execution lease，创建 Run，并返回事件 Iterator。
- `resume_run(run_id)`：检查归属、定义差异和恢复规则后继续原 run_id。
- `cancel_run(run_id)`：包括 worker 已退出的 paused Run，幂等持久化 cancelled。
- `run_snapshot(run_id)`：给服务端读取结构化状态。
- `fork_session(...)`：在公开 user 边界创建一致的新 Session。
- `close()`：唤醒交互、结束执行并关闭 Runtime。

返回的 `RunExecution` 是 context manager。调用方不再消费事件时必须 `close()`，否则 lease 可能迟迟不
释放。

## checkpoint 双槽

RunStore 使用 current/prev 双槽和原子替换。写新 current 前保留上一份有效状态；如果 current 损坏，
可读取 prev。基本思想：

```text
序列化并校验新文档 -> 写同目录临时文件 -> flush/fsync -> os.replace -> fsync 目录
```

它保证文件发布的原子性，但不等于数据库事务，也不能保证外部 API 副作用 exactly once。

## 唯一终态和 Session 同步

Run 进入 terminal 后，Application 把成功的公开消息、compaction checkpoint 和 Artifact 幂等同步进
Session。Session 保存成功后，才把 Run 的 `session_synced` 标为 true。

事件 Iterator 自己负责异常收口：即使 provider 或消费者迭代期间抛异常，Agent 也要持久化 failed，
同步 Session，并产生唯一真实 `run_terminal`。API 不应创建一套 synthetic 状态机。

## execution lease

线程锁只保护一个 Python 进程。API 可能有多个 worker，因此项目还用文件 lease 保证同一 Session 不被
两个进程同时执行。lease 覆盖“开始 Run -> 消费/关闭事件 -> 终态同步”的关键生命周期。

## 恢复时检查什么

- provider、model、system prompt 和 tool schema 定义是否变化。
- checkpoint 是否属于目标 Session/run_id。
- 是否存在 started 但未完成的副作用工具。
- continuation 预算扩展是否已经持久化，不能重复增加。
- 旧 checkpoint schema 是否能安全迁移；无法证明安全时 fail closed。

## Session fork

`fork_session(before_user_message_id, idempotency_key)` 只接受公开 user 边界，并严格排除该 user 及之后
消息。复制范围内的 Artifact 深复制并生成新 ID，公开 `run_id` 置空。操作在源 Session 锁内读取一致
快照，失败不发布半成品；幂等键跨重启有效。

## 关键源码

- `application/runs.py`：Run 用例和事件生命周期。
- `agent/run/coordinator.py`：状态转换唯一入口。
- `agent/run/state.py`：持久化 schema。
- `persistence/run_store.py`：Run 双槽。
- `persistence/store.py`：Session 锁、迁移、ledger 与原子保存。
- `persistence/execution_lease.py`：跨进程 Session lease。

