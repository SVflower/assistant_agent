# M24 受控图表展示与 Artifact 契约

状态：已实施，冻结来源为协调仓库 `M24_CHART_CONTRACT.md@d19dd7b`。

## 范围与决策

- 新增版本化 `ChartSpecV1`，只允许 line、bar、stacked_bar、area、scatter、donut。
- Agent 是完整 Artifact 的唯一权威所有者；API 不复制、不扫描 Session/Run 文件。
- `present_chart` 是纯、幂等工具，复用 Registry、审计和 Run checkpoint。
- `ToolResult/StepEvent` additive 增加 `chart`，不新增 EventKind，事件契约保持 v1。
- Run checkpoint 升 v4，v1-v3 迁移为 `presentations=[]`。
- 完整 Artifact 受硬限内联到 Run/Session 原子状态；删除 Session 级联删除所属 Run。
- 低上下文 Runtime 可安全省略图表工具并产生 notice，不能因此阻止 Runtime 启动。
- 不修改 `agent/loop.py`；不修改 API/Web；不实现 ECharts option、JS/HTML/URL、3D 或业务数据权限。

## 所有权与数据流

```text
model -> present_chart -> ChartSpecV1 validation -> ChartArtifact
      -> RunCoordinator.tool_completed -> RunState v4 atomic checkpoint
      -> StepEvent.tool_result(chart) -> final -> run_terminal
      -> terminal Session sync -> Session presentations/message refs
      -> AgentService.get_artifact / SessionRuntime list|get|snapshot
```

RunCoordinator 是 Run Artifact 限额、冲突和幂等的唯一状态 owner。Session 同步按
`artifact_id + content_hash` 合并，Store 继续使用既有原子替换，不增加第二套 Artifact 文件状态机。

## 安全与限制

- Pydantic strict + `extra=forbid`，禁止任意渲染 option 和可执行配置。
- 12 列、5000 行、20000 cells、8 series；单个 512 KiB；每 Run 16 个/2 MiB。
- canonical UTF-8 JSON 使用排序 key、紧凑分隔符、`allow_nan=False`，hash 为 SHA-256。
- ID 从 session/run/call/hash 确定性派生；同 ID 同 hash 幂等，同 ID 不同 hash 安全拒绝。
- 失败只产生 `artifact_rejected` 工具结果和 notice，不改变 final/run_terminal 语义。

## 测试与验收

- 契约：字段、hash、禁用配置、encoding、类型和固定上限。
- 恢复：v1-v3 到 v4、安全幂等 replay、checkpoint 后再发事件。
- 服务：创建、事件、历史同步、refs、list/get/snapshot、跨 Session 隔离和级联删除。
- Eval：scripted 模型选择 `present_chart`，同时保留完整文字结论。
- 质量门：pytest/coverage、Ruff、mypy、import-linter、scripted/recovery eval。

## 兼容结论

`EVENT_CONTRACT_VERSION` 保持 1；旧调用方忽略可选 `chart` 即可。checkpoint v4 可读取 v1-v3，
旧 Agent 不承诺读取 v4。API/Web 必须按正式服务指南的 M24 章节升级后才启用真实图表 transport。
