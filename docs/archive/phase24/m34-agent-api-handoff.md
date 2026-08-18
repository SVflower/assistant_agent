# M34 Agent -> API 运行可观测性交接

状态：Agent 已实现，等待 API 固定最终 commit 后接入
日期：2026-08-18

## 版本

- `OBSERVABILITY_CONTRACT_VERSION = 1`
- `EVENT_CONTRACT_VERSION = 1`，仅增加可选字段
- `AGENT_SERVICE_CONTRACT_VERSION = 5`，仅增加 DTO 和 snapshot 字段
- Run checkpoint current-only 从 v10 升为 v11；v10 及更旧版本 fail closed，不迁移
- Session v5、Output v1、Chart V2、Attachment/Content v1 均不变

API 必须固定包含 M34 的 Agent commit，并从 `assistant_agent.service` 或
`assistant_agent.contracts` 导入公共 DTO。不得复制类型、解析 checkpoint 或扫描 Run 文件。

## 公共 DTO

`RunObservabilitySnapshot`：

```text
schema_version: 1
timing: TimingSnapshot
context: ContextUsageSnapshot
model_usage: ModelUsageSnapshot
trajectory: tuple[TrajectoryEntry, ...]       # 最多 256 条
task_plan: TaskPlanSnapshot | null             # M34 首批始终 null
truncated: bool
```

`TimingSnapshot`：

```text
run_started_at: string
completed_at: string | null
run_duration_ms: integer >= 0 | null
model_duration_ms: integer >= 0 | null
tool_duration_ms: integer >= 0 | null
interaction_wait_duration_ms: integer >= 0 | null
first_token_latency_ms: integer >= 0 | null
tokens_per_second: finite number >= 0 | null
source: provider | estimated | derived | unavailable
```

`ContextUsageSnapshot`：

```text
used_tokens: integer >= 0 | null
projected_tokens: integer >= 0 | null
limit_tokens: integer > 0 | null
percent: finite number 0..100 | null
source: provider | estimated | unavailable
```

`ModelUsageSnapshot`：

```text
input_tokens: integer >= 0 | null
output_tokens: integer >= 0 | null
cache_read_tokens: integer >= 0 | null
cache_write_tokens: integer >= 0 | null
cache_hit_percent: finite number 0..100 | null
token_source: provider | unavailable
cache_source: provider | unavailable
performance_source: derived | unavailable
```

Provider 未报告 cache 时相关数值必须保持 `null`，不能转换成 0。`run.usage` 与
`observability.model_usage` 共享 Agent usage 事实；API 不得另建累计器。

`TrajectoryEntry`：

```text
entry_id: traj_[a-f0-9]{24}
sequence: integer >= 1
category: run | model | tool | interaction | output | compaction
status: started | streaming | waiting | paused | completed | failed | cancelled
title: string <= 160
started_at: string
completed_at: string | null
duration_ms: integer >= 0 | null
call_id: string | null
tool_name: string | null
result_code: string | null
summary: string <= 1024 | null
```

`TaskPlanSnapshot`/`TaskPlanItem` 已作为预留 DTO 导出，但本阶段没有显式计划写入工具，生产值
始终为 `null`。API 不发布 `run.task_plan`，Web 隐藏计划面板，不得从自然语言或工具轨迹猜测计划。

## StepEvent 与 Snapshot

`StepEvent` additive 增加：

```text
observability: RunObservabilitySnapshot | null
trajectory_entry: TrajectoryEntry | null
```

`RunSnapshot` additive 增加：

```text
observability: RunObservabilitySnapshot | null
```

Agent 在非 `reasoning`/`content_delta` 事件上附加当前 snapshot，并在有轨迹时附加最新 entry。
API 推荐映射：

```text
StepEvent.observability    -> run.observability
StepEvent.trajectory_entry -> run.trajectory（按 entry_id upsert）
StepEvent.usage            -> 现有 run.usage（兼容保留）
RunSnapshot.observability  -> REST RunResponse.observability 权威覆盖
```

`trajectory_entry` 可能是对既有 entry 的状态更新，也可能是新 entry；必须按 `entry_id` upsert，按
`sequence` 排序，不能只 append。断线、reset 或进程恢复后，以 `RunSnapshot.observability` 覆盖 API
投影。首批没有 trajectory 分页接口；`truncated=true` 时如实传给 Web，不创建第二套历史存储。

## 事件序列

纯文本成功：

```text
activity(preparing_context) + observability + trajectory_entry
content_delta*              # 不附 observability，避免高频大 payload
usage + observability + trajectory_entry
final + observability + trajectory_entry
activity(syncing_session) + observability + trajectory_entry
run_terminal(completed) + observability + trajectory_entry
```

工具与 Interaction 仍使用原有 EventKind；trajectory 只补充安全阶段事实。暂停/失败/取消继续由唯一
`run_terminal` 和 Run snapshot 决定，不因观测能力改变终态。

## 安全与兼容

- trajectory 不含 hidden reasoning、prompt/provider 原始 payload、密钥、环境变量、服务器路径、PID、
  完整工具参数、完整工具输出或文件正文。
- 所有缺失指标使用 `null + unavailable`，API/Web 不补造值。
- Event v1 和 Service v5 调用方可忽略新增可选字段；但 API 启用 M34 UI 前必须固定 M34 Agent commit。
- Agent current-only checkpoint 已升 v11。API 不解析版本；部署前应清理旧开发期 Run 状态，不能手改
  checkpoint 版本号。

## API 联调验收

1. REST RunResponse 严格保真 `observability`，所有数值允许 `null`。
2. 实时事件按 `entry_id` upsert，断线重连不重复轨迹。
3. snapshot reset 覆盖本地投影，`truncated` 原样保留。
4. Provider 无 cache/TTFT 时显示不可得，不显示 0。
5. `run.usage` 与 model usage 不重复累计。
6. `task_plan=null` 时不发布伪计划。
7. final 后仍以唯一 `run_terminal` 作为终态。
8. 未知 additive 字段可忽略；版本不匹配继续 fail closed。

## Agent 验证

- M34 核心/契约、RunState v11、恢复、RunStore 和生命周期定向 pytest：102 passed；
- 相关 Ruff format/check：通过；
- 相关 10 个生产文件 mypy：通过；
- 未运行全量 pytest/coverage；未修改 `agent/loop.py`。
