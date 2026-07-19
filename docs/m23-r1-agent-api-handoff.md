# M23-R1 Agent -> API Handoff

状态：Agent 独立复审修复完成，待 API 接入与跨仓联调

## 精确基线与提交

- Agent `main` 基线：`fceb2d49d49f2a816d78675e2d16b06275590461`
- M23-R1 初始实现：`5724bfe0c8541ee0a8f9565c7ba314f65d9f93e8`
- 独立复审修复实现：`919627b942eabc4ac03811334b6c2b73468fa76c`
- 第二轮复审最终实现：`4f932196a9e68269c5d1aaf1ab8320d4895d9632`
- 按 ID summary 补齐基线：`2caa0cc0bcdc09ae92f89837185a38453eaf89f3`
- 按 ID summary 精确实现：`f6bf10bd21d62a32e8f4a641f44c67bef8c42de7`
- 索引完整性复审实现：`ad3550ef6513475970c5b70291c72f2e00443010`
- 索引提交窗口最终实现：`b658371a4322edeec897db31b2937bb7579e4cf4`
- 权威双槽核对最终实现：`9691b177e2ba408e7c2bf875d793518b71c502c1`
- Windows lifecycle 持续争用实现：`5f90c21bea3ca397a728c273b84e61e1f23fc51b`
- Windows 错误分类与 fork 安全最终实现：`d65e103df53a30713562211e09063cfc5cb054ec`
- 分支：`codex/m23-r1-summary-by-id`
- `SESSION_CONTRACT_VERSION=1`、`EVENT_CONTRACT_VERSION=1`、RunState schema v6，未修改 Agent Loop

本文件之后的纯文档提交不改变 API 应集成的 Agent 行为；等待中的 API 应基于上述错误分类与 fork
安全最终实现 commit 集成和验证。

## API 必改项

1. Session detail/get 必须调用 `get_session_summary(session_id)` 取得公开 summary，不得通过 catalog
   分页、Session 文件或 API 自建索引模拟。`SessionNotFoundError` 映射 `404 session_not_found`，
   `SessionUnavailableError` 映射 `503 session_unavailable`。
2. `GET /api/v1/sessions` 必须原样调用
   `catalog_sessions(query=None, limit=30, cursor=None)`，不得自行扫描、排序、搜索或解析 cursor。
3. `PATCH /api/v1/sessions/{session_id}` 必须使用 strict、`extra=forbid` 的
   `{title, expected_metadata_version}`，并调用 `update_session_metadata`；CAS 冲突不得自动重试。
4. 公开响应只映射 `SessionSummary`、`LastRunSummary`、`SessionCatalogPage` 白名单字段。不得暴露路径、
   prompt、reasoning、工具参数/结果、checkpoint、Token 或 Artifact 内容。
5. 稳定映射以下错误：`invalid_session_query`、`invalid_session_limit`、`invalid_session_cursor`、
   `invalid_session_metadata`、`session_not_found`、`session_metadata_conflict`、
   `session_unavailable`。PATCH 非 JSON 仍由 API 返回 `415 unsupported_media_type`。
6. API 不得假设 `force=True` 会等待活动 Run 正常结束。force 删除先发布 tombstone，旧事件消费者可能收到
   `FileNotFoundError`；API 应停止消费并清理 Runtime，不得把它映射成 Session/Run 重新创建。
7. Run 单删同样持久 tombstone。默认删除 running/paused Run 必须返回冲突；force 单删后，API 不得
   重试同 run_id 的 checkpoint、resume 或 create。

## 兼容与持久化影响

- Session schema v1 现在把缺失版本、显式 `schema_version=0`、缺失 v1 元数据字段视作 v0，并在共享锁内
  原子迁移。非法版本类型、负数、未知未来版本 fail closed。
- 新 Session/Run 时间统一为 UTC RFC3339 `Z`。旧 naive 时间冻结解释为 UTC，与机器本地时区无关；
  offset 时间换算为 UTC，合法小数秒不会截断为整秒。catalog、cursor 和 last_run 都按解析后的真实
  UTC instant 比较。
- Session 更新默认具有 `must_exist` 语义。删除发布持久 tombstone 后，旧 Runtime 不能重建 Session 或
  Run checkpoint。普通删除还以 M22 execution lease 封闭“检查后启动”窗口。
- RunStore 首次 save 创建 Run，后续 save 轮转 current/previous。单删先发布独立 Run tombstone 再清理
  双槽；load/list 隐藏、重复删除返回 false、迟到 save 和进程重启后的同 ID save 均稳定失败。
- CLI `sessions --delete` 已改为调用 AgentService 完整删除用例；默认拒绝活动 Run，`--force` 使用与 API
  相同的 Session/Run tombstone 和级联语义。
- metadata CAS 的 last_run 聚合在提交前完成。RunStore/SessionStore 异常稳定映射
  `SessionUnavailableError`；成功提交后不再执行可能失败的 RunStore 读取。
- `get_session_summary` 在 Session lifecycle/document 锁内完成迁移、字段读取、目标 Session last_run 聚合
  和 DTO 构造。DTO 构造完成是线性化点，并发 rename/delete 只能发生在线性化点前或后。
- RunStore 的 `.session-index-v1/manifest.json` 以单文件原子替换选择 generation，并记录每个 Session
  的完整 Run ID 集合；ref 包含可校验的 Session/Run 身份。每进程首次看到一个索引 epoch 时，把完整
  manifest/ref 集合与可加载、未 tombstone、Session-scoped 的权威 current/previous 双槽集合核对；
  自洽遗漏、缺 ref/目录、坏 manifest/ref 或 stale ref 都会在索引锁内重建。无法安全重建时稳定返回
  `SessionUnavailableError`，不能伪装成 `last_run=None`。健康 epoch 的 direct 只做 O(1) epoch 判断和
  目标 Session 查询，不逐请求扫描 Run 根目录。
- catalog 与 direct summary 复用同一个公开有效 Run 状态 helper；未知状态在选择最新 Run 前过滤，不能
  遮蔽较旧的有效 Run。Run delete/prune/tombstone 与 Session cascade 原子、幂等清理对应 ref，但保留
  独立 Run tombstone 防止复活。
- lifecycle 锁使用固定 64 分片，锁文件数量有界；按 ID tombstone 仍持久保留。锁顺序固定为
  `Session lifecycle（如适用） -> index lifecycle -> Run lifecycle -> checkpoint 双槽`。
- Windows lifecycle 锁不使用 `LK_LOCK` 的固定短重试窗口；改用 `LK_NBLCK` 检测正常锁冲突，并以
  50ms 可中断休眠持续等待持有者释放，不设置正常短临界区 timeout。CPython `msvcrt.locking` 实测以
  `errno=EACCES` 且无 `winerror` 表示争用；只有该精确形态重试。`EAGAIN`、`EDEADLK`、WinError 5/36、
  `EBADF`、`ENOSPC` 及未知组合立即原样 fail closed。
  进程内按 shard 使用 `RLock`，同线程重入只由最外层上下文持有 OS 锁；上下文退出、Ctrl-C 异常展开
  或进程终止均释放句柄，Windows/POSIX 既有锁顺序不变。
- 支持 `os.register_at_fork` 的平台还注册 Python `os.fork` audit guard 与 at-fork 回调：当前线程持有任一
  lifecycle 锁时，audit guard 在系统调用前稳定抛 `RuntimeError`；其他线程持锁时，fork 前等待其退出
  临界区。parent 释放 fork 准备锁，child 重建 `RLock` 表和 thread-local，不能继承重入捷径或永久锁。
  注册带模块级幂等标记，reload 不重复注册。该保证只覆盖 Python `os.fork`；Windows 无
  `register_at_fork` 时不注册，`spawn` 仍依赖同一 OS 文件锁正常串行。
- save 先替换 ref，再以 manifest 单文件替换提交索引可见性，最后写权威 checkpoint；这不是跨文件事务。
  索引阶段失败不会留下已提交 checkpoint，checkpoint 失败只留下可检测 stale ref。下一次 direct/startup
  依据 epoch 和权威双槽核对重建，因此已覆盖的进程崩溃点不会永久静默漏 Run。
- 每个临时文件在 replace 前 flush + fsync，replace 后再次 fsync 目标文件；POSIX 还尽力 fsync 必要父目录。
  Windows 使用可移植的目标文件 flush/fsync + `os.replace`，Python 不提供可移植目录 fsync，因此这里只
  声明进程崩溃恢复语义，不承诺断电、控制器缓存或文件系统故障下的绝对持久性。
- 公共 DTO 字段保持 M23-R1 冻结契约；新增 `SESSION_CONTRACT_VERSION=1` 公共导出。Event v1、
  Session schema v1、RunState v6、M22/M24/M25 行为不变。

## 联调测试

1. 按 ID get 对存在、缺失、tombstone、旧 Session 迁移分别返回正确 summary/错误；last_run 使用
   `(parsed updated_at DESC, id DESC)`，字段与 catalog 完全一致。
2. spy 禁止 catalog、Session list、Run list/load 和 Run 根目录扫描；get 只能执行一次锁内 Session 读取与
   一次目标 Session last_run 聚合。
3. 屏障测试证明 summary 构造期间 rename/delete 被 Session 锁阻塞；写先完成时读取新值或 NotFound。
4. catalog 同秒数据按 `(parsed updated_at DESC, id DESC)` 跨页无重复/遗漏；cursor 可换 limit，换 query
   或篡改必须返回 `invalid_session_cursor`。
5. NFKC + casefold 仅搜索 title 和公开 preview；内部消息、工具数据和路径不得命中或出现在响应。
6. 两个并发 PATCH 使用同一 `metadata_version` 时仅一个成功，另一个返回
   `session_metadata_conflict`，且用户标题不被随后 Run 终态覆盖。
7. RunStore 在 CAS 提交前失败时返回 `session_unavailable` 且标题/version 不变；成功提交后不得因额外
   RunStore 读取返回未知失败。
8. 删除检查与并发 Run 启动互斥；force 删除活动 Run 后 Session 文件、Run 双槽和公共列表均消失，旧
   Runtime 的 checkpoint/终态同步不能复活数据。
9. force 单删活动 Run 后放行迟到 checkpoint，必须稳定失败且不生成 current/previous；重复删除、重启
   后 save、Session tombstone 与 Run tombstone 组合均不得复活。
10. 使用 fractional naive、`Z` 和正负 offset 的历史 Session/Run 验证 last_run、catalog 排序和分页在
   不同时区进程环境中结果一致，`.1` 与 `.9` 保持不同 instant，所有 wire 时间均为 UTC `Z`。
11. CLI 默认拒绝含活动 Run 的 Session 删除；force 成功后 Session 与 Run 双槽消失且迟到写失败。
12. 回归 M22 resume/reconcile/retry、M24 Artifact 删除级联、M25 paused cancel 和 Web Runtime profile。
13. 删除单 ref/Session ref 子目录、写坏 manifest/ref、遗留临时文件并重启；direct 必须自愈且与 catalog
    一致。注入 manifest 原子替换失败时必须返回 `session_unavailable`，不能返回空 last_run。
14. 较新的未知 Run 状态不得遮蔽较旧 running/paused/terminal 状态；相同 UTC instant 以 Run ID DESC
    决胜。大量 Run create/delete 后 active generation 不留 ref/temp，lifecycle `.lock` 文件不超过 64，
    `.deleted` tombstone 仍逐 ID 保留。
15. 同时从 manifest/generation/ref 删除最新 Run 但保留 checkpoint，或保留合法 ref 但删除双槽，重启
    必须按权威集合重建且 direct/catalog 一致。ref/manifest replace 后、checkpoint 前模拟进程崩溃也必须
    在重启或新 epoch 首次 direct 时清理 stale ref；同一健康 epoch 后续 direct 不再全目录扫描。
16. Windows 原生执行 2 进程各 120 次连续 Session save、8 进程持续竞争以及 direct/save/delete 混合；
    所有操作不得出现 `EDEADLK` 或固定窗口 timeout。持有者 `os._exit` 后等待者必须取得锁，等待期间 CPU
    时间保持低水平；所有压力测试由父进程设置 15..90 秒防挂死 timeout 并在 finally 清理子进程。
17. Windows 注入裸 `EACCES`、`EACCES+WinError5/36`、`EAGAIN`、`EDEADLK`、WinError36、`EBADF`、
    `ENOSPC`；仅第一种允许重试，其他异常对象必须原样透传且不得 sleep。
18. POSIX 验证当前线程持锁时 `os.fork` 被拒、其他线程持锁时 fork 等待、fork 后 parent/child 继续由
    文件锁串行，以及 reload 后 guard 不重复；Windows/跨平台验证 `spawn` 不回退。所有 child wait 都有
    明确 timeout 和清理路径。

## Agent 验证结果

- Ruff format/check：通过
- mypy：131 source files，通过
- import-linter：12/12
- pytest coverage：783 passed、10 skipped、84%
- scripted eval：19/19
- recovery eval：4/4
- 测试/eval 子进程：无残留；仓库相关监听端口：无

## 风险与边界

- tombstone 持久保留，依赖 Session ID 与 Run ID 不复用；当前 ID 生成规则满足该不变量。未来若引入
  ID 导入/恢复，必须先定义 tombstone 清理或 generation 协议。
- force 删除的消费者错误是删除后的 fail-closed 信号，不是可重试写入信号。API 必须结束对应 Runtime。
- Run 历史时间在读取边界规范化，不回写 checkpoint；Session v1 时间会随迁移原子规范化。运维比较原始
  文件时需考虑这一差异。
- 旧 RunStore 首次创建本版实例、进程首次看到新索引 epoch，或检测到索引不完整时，会锁内扫描一次
  checkpoint 集合并按需重建 generation；健康 epoch 的按 ID get 不做全目录扫描。checkpoint 仍是权威
  事实，manifest/ref 只是可修复索引。
- 本 handoff 不授权修改 Agent 持久文件，也不替代正式契约
  `docs/agent-service-integration-guide.md` 和协调仓库冻结契约。
