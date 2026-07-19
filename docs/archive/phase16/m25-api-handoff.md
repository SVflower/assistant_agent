# M25 Agent -> API 交接

Agent 基线：本文件所在的 `codex/m25-web-runtime` 完成提交。API/Web 不应继续使用通用
`RuntimePolicy.service()` 承载浏览器会话。

## API 必改

1. 创建浏览器 Session Runtime 时显式传入 `RuntimePolicy.web()`；缺失或未知部署 profile 应拒绝启动，
   不能回退 CLI/custom。
2. 读取 `RuntimeCapabilities.profile == "web"`，并用 API 自己的部署 allowlist 再次过滤 tools。
3. Interaction broker 从 `BlockingInteractionPort.next_request()` 取得的 DTO读取 `expires_at`，不得用 API
   本地时间重新猜测。API 默认 interaction timeout 调整为 90 秒，并把同一截止时间用于事件和 snapshot。
4. `interaction.request` 与 `pending_interaction` 使用相同主体：kind/request_id/run_id/session_id/call_id/
   expires_at/data。五类决策选项统一对外命名 `legal_options`，不要再发布 `options`。
5. pause/cancel 继续调用 Agent `SessionRuntime.pause()/cancel()`；M25 会中断待处理 Interaction。API 随后
   清理 broker pending，并发布一次 resolved/notice，cancel 最终只转发一次 run.terminal。
6. Web profile 中 `web_search` 不会再产生 approval。API 不应为了普通查询自行构造授权事件。

## 兼容影响

- `EVENT_CONTRACT_VERSION == 1`，Run checkpoint v4 不变。
- `RuntimeCapabilities.profile`、Interaction `expires_at` 和 question `legal_options` 是 additive 字段。
- CLI 行为不变；非 Web 服务可继续使用 `RuntimePolicy.service()` 或显式 custom policy。
- `fetch_url` 不在 Web profile。API 不得手工加回；需等待 Agent 完成 DNS 地址到实际连接的绑定。
- 通用文件 Export Artifact 尚未实现。API 只能继续消费 M24 Chart Artifact，不得暴露 Agent 工作目录。

## 联调序列

```text
create Session(RuntimePolicy.web)
-> capabilities(profile=web, tools includes web_search, excludes run_shell/read_file/fetch_url)
-> start Run
-> tool_call(web_search)
-> tool_result
-> final
-> run_terminal(completed)
```

高后果专用业务工具仍应产生 approval：

```text
interaction.request(..., expires_at, data.legal_options)
-> waiting_interaction
-> response / pause / cancel / timeout
-> interaction.resolved or timeout notice
-> exactly one run_terminal when terminal
```

API 契约测试至少覆盖：真实字段映射、90 秒截止时间、错误/过期/重复 response、refresh snapshot 恢复、
等待期间 pause/cancel，以及 capabilities 双重过滤。
