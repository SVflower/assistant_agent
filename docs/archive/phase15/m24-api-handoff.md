# M24 Agent -> API 交接

> 最终 Agent commit 在本里程碑提交后填写到交付消息；本文件记录冻结接口，不替代正式契约
> `docs/agent-service-integration-guide.md`。

API 必改：

1. 仅从 `assistant_agent.service` 导入 Chart DTO/异常，不读取 Agent 内部文件。
2. 消费成功 `tool_result.chart`，只向 WebSocket 发 Artifact summary，事件名
   `assistant.artifact`；不要把完整 rows 放进事件缓存。
3. 新增 `GET /api/v1/sessions/{session_id}/artifacts/{artifact_id}`，调用
   `AgentService.get_artifact()` 返回完整 Artifact。
4. Message/Run snapshot additive 增加 `artifacts=[]`；Message `id` 允许旧历史为 null。
5. `artifact_not_found` 映射 404；`artifact_unavailable` 映射 503；不返回 path/异常/原始参数。
6. 保持 `tool_call -> tool_result(chart) -> final -> run_terminal`；图表失败不得改变正文和终态。
7. Web event contract 可保持 v2，前提是旧客户端忽略未知 `assistant.artifact`。

联调必须覆盖：成功图表、刷新历史、断线事件重放后 REST 拉取、跨 Session 404、删除级联、未知
schema、非法 encoding 表格降级、Artifact 拒绝后正文完成，以及低上下文 Runtime 没有
`present_chart` 时 API 仍 ready。
