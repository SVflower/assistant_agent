# M34 DeepSeek Harness 运行可观测性吸收方案

状态：Agent 侧已完成
日期：2026-08-17

## 目标与边界

吸收 DeepSeek Harness 的 Conversation/Trajectory 分离、阶段耗时、上下文压力和模型用量表达，
但继续由现有 `RunCoordinator` 和 Run checkpoint 持有唯一权威状态。不引入 Cordis、插件事件总线、
JSONL 事件溯源或浏览器高权限能力，不展示 hidden reasoning，不改变工具权限、终态和恢复语义。

本阶段实现：

- `RunObservabilitySnapshot`、`TimingSnapshot`、`ContextUsageSnapshot`、
  `ModelUsageSnapshot`、`TrajectoryEntry` 和可空 `TaskPlanSnapshot` 公共 DTO；
- RunState v11 内的有界、可恢复观测事实；
- `RunSnapshot.observability` 权威恢复入口；
- 既有 `ItemEvent` 上 additive 的 `observability` 与 `trajectory` 可选字段；
- Provider usage 的 cache token 和可靠流式计时归一化。

本阶段不实现：

- task plan 写入工具。没有显式整表写入事实时 `task_plan=null`；
- trajectory 分页接口。Snapshot 仅返回最多 256 条有界尾部并标 `truncated`；
- OTel、计费、跨节点遥测、自然语言计划推断。

## 契约

`OBSERVABILITY_CONTRACT_VERSION=1`。所有 DTO strict、frozen、`extra=forbid`。

- 数值来源仅允许 `provider | estimated | derived | unavailable`；不可得值为 `null`。
- Context 的 `used_tokens` 优先采用最近一次 provider prompt usage，否则采用现有 context estimator；
  `projected_tokens` 始终是当前表层的估算值，输出 token 不计入上下文压力。
- Model usage 沿用既有 `ItemEvent.usage`。同一模型 step 的后续 usage 替换前值，再与已完成 step 累计，
  防止流式分片重复累加。cache 未报告时保持 `null`。
- Timing 用进程内 monotonic 计算并固化毫秒；UTC 仅用于展示。重启间隔不通过 wall clock 倒推。
- Trajectory 只记录 `run/model/tool/interaction/output/compaction` 安全事实；不保存参数、输出正文、
  prompt、provider payload、路径、PID 或 reasoning。`entry_id` 由 run_id 与单调 sequence 确定。
- Task plan 仅预留显式整表快照 DTO，本阶段始终为 `null`。

## 所有权与实现

1. `contracts/observability.py` 持有公共 DTO。
2. `agent/run/observability.py` 持有纯运行记录器；`RunCoordinator` 在既有状态转换点驱动记录器，
   并通过原 checkpoint 保存，不建立第二套状态机。
3. `application/runs.py` 在既有事件迭代边界更新 context/usage，并把当前权威 snapshot 或单条
   trajectory 附加到 ItemEvent；`run_terminal` 仍且只发送一次。
4. `providers/litellm.py` 只归一 Provider 明确报告的 cache token，并用 monotonic 记录请求、首个
   可见内容或 tool-call fragment、usage 到达时间。未报告 usage 时不伪造 token。

## 版本与兼容

- Agent Event contract 保持 v1：只增加可选字段，不增加 `EventKind`。
- Agent Service contract 保持 v5：`RunSnapshot` additive 增加可选字段，新 DTO 新增导出。
- Run checkpoint 升 v11。项目已采用 current-schema hard cut，v10 及更旧状态 fail closed，不迁移。
- API 必须固定包含 M34 的 Agent commit 后再映射新字段；旧 API 忽略 additive 字段仍可运行。

## 测试与验收

- DTO strict/null/source/有限数与 256 条上限；
- 文本 Run 的模型 timing、usage、context 和唯一 terminal；
- 工具 started/completed 使用同一 entry_id，且不泄漏参数/输出；
- usage 分片替换、cache 未报告为 null、cache 报告正确；
- pause/cancel/interaction timing；
- checkpoint 恢复不重复 trajectory，重启间隔不伪造耗时；
- RunSnapshot 与 ItemEvent additive 契约、v11 hard cut；
- 定向 pytest、涉及文件 Ruff、涉及包 mypy。

## 风险

- 不同 OpenAI-compatible Provider 的 cache 字段不一致，只接受明确白名单字段。
- 无 usage 的 Provider 无法给出精确 token；context 降级为 estimator 并标来源。
- 首批不提供 trajectory 分页，超过 256 条只保留有界尾部并明确 truncated。
- TaskPlan 没有可靠写入面，本阶段保持 null，避免从回答文本猜测。

## 验收结果

- M34 核心/契约、RunState v11、恢复、RunStore 和生命周期定向测试共 102 项通过；
- 涉及文件 Ruff format/check 与 10 个相关生产文件 mypy 通过；
- 未运行全量 pytest/coverage，符合本里程碑按需测试约束；
- `agent/loop.py` 未修改；Event v1、Service v5、Session v5 保持不变。
