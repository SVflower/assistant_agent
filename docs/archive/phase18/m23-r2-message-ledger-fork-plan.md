# M23-R2 权威消息 Ledger 与 Session Fork 方案

> 状态：已实施；冻结事实源为协调仓库 `M23_CONVERSATION_CONTRACT.md` 与
> `M23_CONVERSATION_PLAN.md`。本文件记录 Agent 仓落地决定，不替代长期服务契约。

## 范围

- Session schema v1/v0 锁内原子迁移到 v2，建立所有公开 user/assistant 消息的权威 ledger。
- 提供绑定源 `SessionRuntime` 的 `fork_session(before_user_message_id, idempotency_key)`。
- 深复制 fork 范围内 Chart Artifact，重绑定 Session/message，公开 `run_id=null`。
- 持久化跨重启幂等身份，并以一个完整目标 Session 文档原子发布。
- 更新 Session snapshot、catalog/count/preview、公共错误和 API 正式契约。

不修改 Event v1、RunState v6、Agent Loop、API/Web，也不提供原地消息编辑或自由历史复制入口。

## 设计

1. `Session.message_ledger` 是公开历史唯一权威；模型 `messages` 只服务 provider/context，compaction
   不得反向改写 ledger。
2. 新 Run 在终态 Session 同步时显式追加 user/assistant ledger 项。ID 由 Session/Run 域和稳定身份
   生成 96-bit SHA-256 前缀，满足 `msg_[a-f0-9]{24}` 并跨 Session 隔离。
3. v0/v1 迁移在既有 lifecycle/document 锁内完成，一次原子替换；可信消息时间规范化为 UTC，未知为
   null；无可证明 user 归属的 assistant 历史拒绝迁移。
4. fork 在源 Session 锁内读取一致快照。边界只接受公开 user ID，复制范围严格排除边界及之后内容；
   目标不复制 Run、Interaction、compaction checkpoint。
5. 幂等 identity 内联目标的 `fork_origin`，避免“目标 + 独立幂等记录”的跨文件双写。相同 key 重启后
   扫描完整目标恢复；匹配结果损坏 fail closed。
6. fork Artifact 复制已校验 ChartSpec，重新计算目标 artifact ID/size，保留 content hash，设置新
   Session/message、`run_id=null` 和提交时间。

## 修改边界

- 公共契约：`contracts/sessions.py`、`contracts/charts.py`、`contracts/errors.py` 及稳定导出。
- 应用层：`application/models.py`、`ports.py`、`runs.py`、`sessions.py`。
- 持久化：`persistence/store.py`、新增 `persistence/session_fork.py`。
- 测试：迁移、边界、compaction、Artifact、幂等、并发、故障注入、公共导出。
- 文档：长期服务契约、架构、技术债、路线图、状态与 API handoff。

## 验收

- 第一/中间/末尾 user 边界严格排除；跨 Session/assistant/非法 ID fail closed。
- ID、reply、时间在迁移、重启、snapshot、catalog 和 compaction 后稳定。
- 相同 key 同请求跨重启重放；异参冲突；并发只发布一个目标。
- 发布失败不留下 catalog 可见目标、悬空 Artifact 或独立幂等记录。
- 源 Session 除 schema v2 迁移外，公开内容、元数据、Run、Interaction、Artifact 不变。
- Ruff、mypy、import-linter、pytest+coverage、scripted/recovery eval 按仓库 DoD 验收。

## 风险

- legacy 历史缺少可证明 user 归属时不能安全生成 reply，返回 `session_migration_required`。
- 幂等重放当前以 Session 目录有界扫描恢复；规模显著增长后可增加同事务索引，但不得建立第二权威。
- `application/runs.py` 与 `persistence/store.py` 超过 600 行，只触发职责评审，不机械拆分。
