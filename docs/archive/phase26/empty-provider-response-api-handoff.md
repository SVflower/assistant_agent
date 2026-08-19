# 空 Provider 响应 Agent -> API 交接

## 固定语义

- Agent commit：以本任务最终报告的本地 `main` 完整 commit 为准。
- 公共版本不变：Service v5、Session contract/schema v5、RunState v11、Event v1。
- `FailureCode` additive 增加 `provider_empty_response`。
- Provider 正常结束且无文本、无 `tool_calls` 时，Agent 仅在同一模型轮次内部重试一次。
- 连续两次为空时，Agent 持久化 failed Run，并发布唯一真实 `run_terminal`。
- 修正提示不持久化；已完成工具、Chart/Output Artifact 和消息历史不重放、不删除。
- 未修改 `agent/loop.py`。

## API 必改

1. 允许并透传 `failure.code == "provider_empty_response"`，不要降级为未知事件或 `internal_error`。
2. 映射为可重试的模型空响应失败：`phase=calling_model`、`retryable=true`、
   `allowed_actions=[retry_run, stop]`。
3. 只以 Agent 的 `run_terminal` 更新权威终态；不得因无 `final` 事件合成 completed，也不得发布第二个
   synthetic terminal。
4. 不解析 `safe_message`，不在 API worker 内自动重复模型调用。显式重试继续调用 Agent 的既有
   `retry_failed_run` 公共用例。
5. 失败 Run 仍可包含此前成功生成的 Artifact/Output；API 不因 terminal=failed 删除或隐藏这些引用。

## 联调验收

1. Provider 首次空、第二次成功：同一 run_id completed，只有一次 terminal。
2. Provider 连续两次空：无“模型未返回内容”assistant 消息，无 final，唯一 failed terminal 携带稳定 code。
3. `ask_user -> inspect_runtime -> 连续空`：两个工具各执行一次，工具历史可恢复，Run failed。
4. `present_chart -> 连续空`：Run failed，但 Chart Artifact 仍可通过公共 Session/Run 接口读取。
5. 第一次空后 pause/cancel：按 paused/cancelled 收口，不出现 `provider_empty_response`。
