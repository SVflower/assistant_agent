# M27 Agent -> API 交接

## 结论

API 必改项：无。

Agent 的 `present_chart` 现在可接收省略 `columns[].data_type` 的模型草稿，并在 Agent 内部归一化为
原有严格 `ChartSpecV1`。成功后的 `ToolResult.chart`、`ItemEvent.chart`、Artifact hash、Session/Run
snapshot 与下载读取链路完全不变。

## 兼容性

- Event contract：v1，不变。
- RunState/checkpoint：v6，不变，无迁移。
- ChartSpec：v1，不变，没有 ChartSpecV2。
- 严格 cloud 风格参数继续兼容；LM Studio 等模型不需要 API 侧标识或分流。
- 图表草稿第一次无效时 `artifact_rejected` 可重试一次；第二次为不可重试。两者仍是非 terminal
  工具失败，API 不得把它升级为 Run failed。
- 图表失败保留文字回答；terminal 仍由 Agent 唯一发送。

## API 联调检查

1. 使用 Agent 本次 main commit 固定依赖。
2. 提交会产生缺失 `data_type` 草稿的本地模型请求，确认仍收到原有 `tool_result.chart`。
3. 连续两次无效草稿，确认 Web 展示安全工具错误，随后仍收到 final 和单个 completed terminal。
4. API/Web 不解析 `[chart_input_invalid]` 文本决定行为；继续使用事件内 failure/retryable 事实。
