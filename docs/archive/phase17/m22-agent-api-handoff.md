# M22 Agent -> API 交接

Agent commit：以 M22 分支最终提交为准。

## API 必改

1. 固定包含 M22 的 Agent commit，只从 `assistant_agent.service` 导入公共类型。
2. `RunSnapshot` 直接映射新增字段：`session_id/status/phase/updated_at/preview/terminal_status/`
   `failure/current_phase/budget/pending_interaction/final_candidate/artifacts/allowed_actions/`
   `execution_status/retry_of_run_id`。
3. `POST /runs/{id}/reconcile` 要求 Idempotency-Key，调用
   `SessionRuntime.reconcile_orphaned_run()`；不得修改 checkpoint。
4. `POST /runs/{id}/retry` 要求 Idempotency-Key，调用
   `SessionRuntime.retry_failed_run()`；使用返回的 original/new Run ID 和 created。
5. `resume_run()` 只接受 paused。持久 running 且 API 无 worker 时引导 reconcile。
6. 映射稳定错误码：`run_still_active`、`run_not_found`、`run_not_resumable`、
   `run_not_reconcilable`、`run_not_retryable`、`run_recovery_required`、
   `idempotency_conflict`、`session_busy`。
7. API 重启后 EventHub 不可恢复时仍读取 Agent RunSnapshot；API 不合成 terminal 或 allowed_actions。

## 兼容

- `EVENT_CONTRACT_VERSION` 保持 1，ItemEvent 顺序不变。
- checkpoint 升至 v6；v1-v5 自动迁移，但 `retry_safety=unknown` 且无可靠会话基线，默认不开放 retry。
- v6 checkpoint 不支持旧 Agent 降级读取。
- 单机文件锁路径不包含业务事实，锁文件可长期存在，不得据其内容判断状态。

## 联调

- 强杀 API 后重启：旧 ticket 失效，Run snapshot 可读，遗留 running 只能 reconcile；
- 同时请求 reconcile/retry：最多一个执行者，相同 retry key 不创建第二个 Run；
- uncertain side effect 返回 recovery required，不显示普通 retry；
- 每个 Run 最多一个 run_terminal，retry 新 Run 保留 `retry_of_run_id`。
