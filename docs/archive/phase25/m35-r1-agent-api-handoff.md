# M35-R1 Agent -> API 交接

## 固定范围

- Agent 实现提交：以交付报告中的本地 main 完整 commit 为准；禁止固定到 M35 前的基线。
- 公共版本不变：Service v5、Session contract/schema v5、RunState v11、Observability v1、Event v1。
- 这是 additive 查询能力，不新增事件种类，不修改 Agent Loop。

## API 必改

1. Session 公共消息 DTO 增加 `run_id: string | null`，逐字映射
   `PublicMessageSnapshot.run_id`。不得按数组位置、文本、时间或 reply 关系猜测。
2. Run DTO 增加 `created_at: string` 与
   `execution_model: { provider: string; model: string } | null`。不得附加 key、base URL 或 provider
   payload。
3. 历史消息的 Run 详情入口只在 `message.run_id != null` 时展示；点击后通过该消息所属
   `SessionRuntime.run_snapshot(run_id)` 查询。ownership 失败按 not-found 处理，不能跨 Session 查询。
4. Run 详情继续使用权威 snapshot 中的 observability、failure、budget、outputs 和 artifacts；API
   不解析 checkpoint，不复制 Agent 状态机，不从事件缓存重建第二套权威状态。
5. fork 返回的复制消息 `run_id` 必须保持 `null`；API/Web 不得继承源会话关联。

## 保留与降级

- 旧消息或不可证明来源的消息为 `run_id=null`，Web 隐藏 Run 详情入口，不显示错误占位。
- 默认 `RecoveryConfig.max_completed_runs=100` 是 RunStore **全局** terminal Run 保留上限，非每 Session。
- trajectory 每 Run 最多 256 条，超限时 `truncated=true`；首批无分页接口。
- Run 已被保留策略清理时，消息仍可存在但查询返回 not-found。文字、Artifact 和 Output 历史不因此丢失。
- R1 不提供完整长期 TraceStore、Session trajectory 时间线分页或敏感工具参数/输出。

## 联调验收

1. 新建 Run 后 user 与 assistant 消息携带同一 `run_id`，刷新/重启后不变。
2. 点击 assistant 或其对应 user 消息可恢复同一 Run 的时间、模型、trajectory、usage 和终态。
3. `run_id=null` 的旧消息和 fork 消息不出现详情入口。
4. 伪造其他 Session 的 `run_id` 不能取得 RunSnapshot。
5. 重复 terminal sync 不产生重复消息，也不改变已有 `run_id`。
6. Web 明示 trajectory `truncated`，且不将 100/256 限制描述为完整长期历史。
