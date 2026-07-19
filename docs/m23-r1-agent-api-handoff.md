# M23-R1 Agent -> API Handoff

状态：Agent 独立复审修复完成，待 API 接入与跨仓联调

## 精确基线与提交

- Agent `main` 基线：`fceb2d49d49f2a816d78675e2d16b06275590461`
- M23-R1 初始实现：`5724bfe0c8541ee0a8f9565c7ba314f65d9f93e8`
- 独立复审修复实现：`919627b942eabc4ac03811334b6c2b73468fa76c`
- 分支：`codex/m23-r1-conversation-catalog`
- `EVENT_CONTRACT_VERSION=1`、RunState schema v6，未修改 Agent Loop

本文件之后的纯文档提交不改变 API 应集成的 Agent 行为；API 应至少基于上述复审修复实现验证。

## API 必改项

1. `GET /api/v1/sessions` 必须原样调用
   `catalog_sessions(query=None, limit=30, cursor=None)`，不得自行扫描、排序、搜索或解析 cursor。
2. `PATCH /api/v1/sessions/{session_id}` 必须使用 strict、`extra=forbid` 的
   `{title, expected_metadata_version}`，并调用 `update_session_metadata`；CAS 冲突不得自动重试。
3. 公开响应只映射 `SessionSummary`、`LastRunSummary`、`SessionCatalogPage` 白名单字段。不得暴露路径、
   prompt、reasoning、工具参数/结果、checkpoint、Token 或 Artifact 内容。
4. 稳定映射以下错误：`invalid_session_query`、`invalid_session_limit`、`invalid_session_cursor`、
   `invalid_session_metadata`、`session_not_found`、`session_metadata_conflict`、
   `session_unavailable`。PATCH 非 JSON 仍由 API 返回 `415 unsupported_media_type`。
5. API 不得假设 `force=True` 会等待活动 Run 正常结束。force 删除先发布 tombstone，旧事件消费者可能收到
   `FileNotFoundError`；API 应停止消费并清理 Runtime，不得把它映射成 Session/Run 重新创建。

## 兼容与持久化影响

- Session schema v1 现在把缺失版本、显式 `schema_version=0`、缺失 v1 元数据字段视作 v0，并在共享锁内
  原子迁移。非法版本类型、负数、未知未来版本 fail closed。
- 新 Session/Run 时间统一为 UTC RFC3339 `Z`。旧 naive 时间冻结解释为 UTC，与机器本地时区无关；
  offset 时间换算为 UTC。catalog、cursor 和 last_run 都按解析后的 UTC instant 比较。
- Session 更新默认具有 `must_exist` 语义。删除发布持久 tombstone 后，旧 Runtime 不能重建 Session 或
  Run checkpoint。普通删除还以 M22 execution lease 封闭“检查后启动”窗口。
- metadata CAS 的 last_run 聚合在提交前完成。RunStore/SessionStore 异常稳定映射
  `SessionUnavailableError`；成功提交后不再执行可能失败的 RunStore 读取。
- 公共 DTO 和方法签名保持 M23-R1 冻结契约；Event v1、RunState v6、M22/M24/M25 行为不变。

## 联调测试

1. catalog 同秒数据按 `(parsed updated_at DESC, id DESC)` 跨页无重复/遗漏；cursor 可换 limit，换 query
   或篡改必须返回 `invalid_session_cursor`。
2. NFKC + casefold 仅搜索 title 和公开 preview；内部消息、工具数据和路径不得命中或出现在响应。
3. 两个并发 PATCH 使用同一 `metadata_version` 时仅一个成功，另一个返回
   `session_metadata_conflict`，且用户标题不被随后 Run 终态覆盖。
4. RunStore 在 CAS 提交前失败时返回 `session_unavailable` 且标题/version 不变；成功提交后不得因额外
   RunStore 读取返回未知失败。
5. 删除检查与并发 Run 启动互斥；force 删除活动 Run 后 Session 文件、Run 双槽和公共列表均消失，旧
   Runtime 的 checkpoint/终态同步不能复活数据。
6. 使用 naive、`Z` 和正负 offset 的历史 Session/Run 验证 last_run、catalog 排序和分页在不同时区进程
   环境中结果一致，所有 wire 时间均为 UTC `Z`。
7. 回归 M22 resume/reconcile/retry、M24 Artifact 删除级联、M25 paused cancel 和 Web Runtime profile。

## Agent 验证结果

- Ruff format/check：通过
- mypy：131 source files，通过
- import-linter：12/12
- pytest coverage：726 passed、6 skipped、84%
- scripted eval：19/19
- recovery eval：4/4
- 测试/eval 子进程：无残留；仓库相关监听端口：无

## 风险与边界

- tombstone 持久保留，依赖 Session ID 不复用；当前 ID 生成规则满足该不变量。未来若引入 Session ID
  导入/恢复，必须先定义 tombstone 清理或 generation 协议。
- force 删除的消费者错误是删除后的 fail-closed 信号，不是可重试写入信号。API 必须结束对应 Runtime。
- Run 历史时间在读取边界规范化，不回写 checkpoint；Session v1 时间会随迁移原子规范化。运维比较原始
  文件时需考虑这一差异。
- 本 handoff 不授权修改 Agent 持久文件，也不替代正式契约
  `docs/agent-service-integration-guide.md` 和协调仓库冻结契约。
