# M25-AGENT-02 Run 终态一致性修复

状态：已实施，未修改 `agent/loop.py`，Event v1 与 checkpoint v4 保持不变。

## 问题与边界

Web E2E 暴露两条公共服务所有权缺口：worker 已退出后，`cancel()` 无法把 paused checkpoint 推进为
cancelled；事件 Iterator 的未分类异常会越过 Agent，使上层合成 failed terminal，而 Agent 仍保存
running/paused。修复必须由既有 `RunCoordinator` 状态机完成，不允许 API 解析 checkpoint、复制同步逻辑
或通过 force-delete 绕过未完成 Run。

## 设计

- `SessionRuntime.cancel_run(run_id)` 只处理当前 Session 中 worker 已退出的 paused Run。
- paused 首次取消调用 `RunCoordinator.cancel()` 原子保存 cancelled/terminal，再用
  `sync_terminal_session()` 幂等同步 Session，最后返回一个真实 `run_terminal(cancelled)`。
- 已 cancelled 的重复调用只补做未完成的 Session 同步，返回空事件流，不重复发布 terminal。
- active/running Run 拒绝离线取消，仍由活跃 worker 配合 `cancel()` 结束，避免第二个消费者。
- completed/failed 和错误 Session 归属拒绝改写。
- 公共 `_stream()` 捕获普通 `Exception`，写入脱敏 `internal_error` failed checkpoint，再走统一 Session
  同步与唯一 terminal；不捕获 `GeneratorExit`、`KeyboardInterrupt`、`SystemExit`，消费者主动关闭仍安全
  paused。

## 验收

- 关闭并重新载入 Session 后，可按 `run_id` 取消 paused Run，Session 随后可正常删除。
- 重复取消幂等，active/错误归属/既有终态不被改写。
- Iterator 异常只产生一次真实 failed terminal，checkpoint 为 failed/terminal 且
  `session_synced=true`，原始异常和敏感字面量不进入公共事件或 checkpoint。
- CLI 既有 pause/cancel/resume 行为不变；不新增 force-delete。

## 公共契约影响

这是 `SessionRuntime` 的 additive 方法，不提升 Event contract。Agent 重新成为 Run 终态唯一权威；API
必须消费 Agent terminal，不再合成 `run.terminal` 或自行推进 checkpoint。完整接入要求见
`docs/agent-service-integration-guide.md` 和 `m25-agent-02-api-handoff.md`。
