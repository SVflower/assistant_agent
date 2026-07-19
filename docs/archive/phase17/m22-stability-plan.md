# M22 Agent 稳定性收口

状态：已实施

## 范围

- 单机跨进程 Session execution lease，覆盖 start/resume/cancel-paused/reconcile/retry；
- RunState checkpoint v5，v1-v4 fail-closed 迁移；
- orphaned running Run 幂等协调；
- failed Run 安全、显式、幂等重试，新建 Run 并保留来源关联；
- 完整公共 RunSnapshot、稳定错误码、公共导出和 API 交接；
- 不修改 Agent Loop，不增加多节点租约、API 事件持久化或 Web UX。

## 不变量

1. Agent 是 Run 状态、恢复资格、重试资格和 terminal 的唯一权威。
2. 活跃执行迭代器持有 Session 文件锁直到结束；进程崩溃由 OS 释放锁。
3. running Run 不能直接 resume，必须先取得租约并 reconcile 为 paused。
4. terminal Run 不原地复活；retry 创建新 Run ID，原 Run 保持不变。
5. v1-v4 没有累计副作用事实，迁移后 `retry_safety=unknown`，禁止普通重试。
6. started 未决工具进入 `tool_uncertain`；事件源异常不得清空该事实。

## 验收

- 同进程和真实子进程租约竞争；
- 活跃执行器拒绝 reconcile，崩溃遗留 running 幂等转 paused；
- safe/unsafe/uncertain/unknown 重试矩阵；
- 重试幂等记录、来源关联、Session busy 和稳定错误；
- v1-v4 到 v5、双槽回退、Session 同步和唯一 terminal 回归；
- Ruff、mypy、12/12 import-linter、pytest coverage、scripted/recovery eval 全绿。

## 风险

- 文件锁只保证同一台机器、同一状态目录；多节点部署必须替换为外部原子租约/CAS。
- `application/runs.py` 继续集中维护 Session/Run 状态不变量，已超过 600 行，评审见
  `docs/ARCHITECTURE.md` 和 D25。
