# M28 Agent -> API/Web 交接

## 版本与兼容结论

- Agent pin：使用总指挥收到的 M28 功能分支最终 commit，不要 pin 持续移动的分支。
- `EVENT_CONTRACT_VERSION = 1`：不变。
- `SESSION_CONTRACT_VERSION = 3`：由 2 升级；Session 文档 schema v3。
- Run checkpoint schema：v7；v1-v6 由 Agent 迁移，API 不读写 checkpoint。
- `ChartSpecV1`、V1 canonical JSON/hash/Artifact：逐字节兼容。
- 新增 `ChartSpecV2` / `ChartArtifactV2` / `PresentationArtifactRefV2`。
- `RuntimeCapabilities.chart_spec_versions` 在图表可用时返回 `(1, 2)`。

## API 必改

1. 将完整 Artifact 响应从只接受 V1 改为按 `schema_version` 判别的 V1/V2 union；未知版本安全返回
   unsupported/fallback，不能让 Session 或文字消息整体失败。
2. 将 message/run artifact summary 的 `schema_version` 允许值扩展为 1/2，继续只在事件中发送 ref，
   完整 spec 仍通过 `get_artifact(session_id, artifact_id)` 获取。
3. 启动时固定 `SESSION_CONTRACT_VERSION == 3`；保真透传 Agent 的 message/artifact refs，不生成 ID、
   不扫描 Agent 文件、不复制迁移逻辑。
4. API WebSocket 映射保持：成功 `tool_result.chart -> assistant.artifact`；EventKind 不新增，
   `final/run_terminal` 顺序不变。
5. V2 full DTO 保真映射 `datasets/layout/panels/derivations`。不得转换或接受任意 ECharts option、
   formatter、HTML、URL、graphic、JS/function/style。
6. API 不识别 cloud/local，也不解析模型草稿；归一化和确定性计算全部属于 Agent。

## Web renderer 必改

- 以受控映射支持 15 种 chart_type、最多 4 panel、每 panel 最多 2 个 Y 轴；只读取白名单字段。
- 提供图表/表格切换、tooltip、legend、zoom、fullscreen、CSV/JSON 下载。
- histogram/boxplot/percent 的 derived dataset 是 Agent 权威结果，Web 不重新计算。
- 未知 mark/axis/ref 或损坏 Artifact 只降级当前图表为表格/不可用提示，不移除文字回答，不伪造
  Run failed。
- V1 renderer 保持原路径，确保历史图表逐字节兼容。

## V2 公共形状

```text
ChartArtifactV2 = PresentationArtifactRefV2 + { spec: ChartSpecV2 }
ChartSpecV2 = {
  schema_version: 2,
  title, description?, source_label?,
  datasets: TabularDatasetV1[1..4],
  layout: {columns: 1|2, panel_order, shared_legend},
  panels: ChartPanelV1[1..4],
  derivations: DerivationTraceV1[]
}
```

精确字段和约束以安装 commit 导出的 `assistant_agent.contracts` Pydantic 类型为事实源。Artifact 不暴露
服务器 path，只暴露 opaque `artifact_id`。

## 联调事件序列

成功：

```text
tool_call(call_id, tool_name=present_chart)
tool_result(call_id, chart=ChartArtifactV2)
final(text)
run_terminal(completed)
```

图表失败但文字成功：

```text
tool_call(present_chart)
tool_result(is_error=true, code=artifact_rejected, chart=null)
[最多一次修正调用]
final(text)
run_terminal(completed)
```

API 必须验证：V1/V2 历史恢复、fork 后 V2 Artifact 新 ID/新 Session/run_id null、删除级联、未知版本
局部降级、断线重放不携带完整 rows，以及文字/final/terminal 不受图表失败影响。
