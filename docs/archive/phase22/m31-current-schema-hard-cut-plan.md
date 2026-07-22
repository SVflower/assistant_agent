# M31 当前 Schema Hard Cut 方案

## 背景与目标

项目仍在开发期，旧 checkpoint、Session 和 Chart V1 的逐级迁移扩大了状态空间，也让损坏数据与旧数据
难以区分。M31 只保留当前格式：RunState v7、Session v3、ChartSpec/ChartArtifact V2。调用方在升级前
应清空本地测试状态，不再依赖运行时迁移。

## 实现边界

- Run checkpoint 读取和写入只接受 `schema_version=7`；删除 v1-v6 migration。
- Session 读取和写入只接受 `schema_version=3`；删除 v0-v2 构造与回写迁移。
- 图表公共 DTO、解析、工具生成和事件只接受 V2；删除 V1 类、builder、联合类型和双路径工具逻辑。
- 版本不匹配分别抛出带稳定 code 的类型化异常：
  `unsupported_run_state_schema`、`unsupported_session_schema`、`unsupported_chart_schema`。
- 不修改 `agent/loop.py`，不修改 API/Web，不保留旧路径 re-export。

## 安全与兼容

- 不兼容数据不能被 catalog 静默跳过、转换为空数组或用默认值补齐。
- 错误只包含 expected/actual version 和安全说明，不包含 checkpoint、Session 或 Artifact 原文。
- Event contract 外壳仍为 v1；这是持久化和 Chart payload 的破坏性 hard cut，下游必须同步固定新 commit。
- 用户应删除测试用 `ASSISTANT_AGENT_HOME/workspaces` 后重新创建 Session；生产数据迁移工具不在本期。

## 测试

- Run v7、Session v3、Chart V2 正常读写、恢复、fork、事件和 artifact 完整性。
- v1-v6 Run、v0-v2 Session、Chart V1 和未来版本均返回精确稳定错误码。
- catalog/get/list/fork 不静默迁移或回写旧数据。
- `present_chart` 只生成 V2，模型输入 schema 不再公开版本 1。
- Ruff、mypy、12 条 import-linter、全量 pytest/coverage、scripted/recovery eval。

