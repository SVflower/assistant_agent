# M31 Agent -> API/Web Hard Cut 交接

## 固定依赖

- Agent commit：以 M31 功能分支最终完整 commit 为准。
- `AGENT_SERVICE_CONTRACT_VERSION = 2`（破坏性升级）。
- `EVENT_CONTRACT_VERSION = 1`。
- Session contract/schema：v3，且只接受 v3。
- RunState/checkpoint：v7，且只接受 v7。
- ChartSpec/ChartArtifact：只公开和接受 V2。

## API 必改

1. 启动时断言 `assistant_agent.service.AGENT_SERVICE_CONTRACT_VERSION == 2`。
2. 删除 `ChartSpecV1`、`ChartArtifact`、`PresentationArtifactRef` 和 `Any*` 联合的导入、DTO 与分支。
3. 图表事件只解析 `ChartArtifactV2`；收到其他 schema 不得降级猜测或迁移。
4. 将以下类型化异常按 `code` 映射为稳定 409/422 类响应，不解析 message：
   - `unsupported_run_state_schema`
   - `unsupported_session_schema`
   - `unsupported_chart_schema`
5. 不复制 checkpoint/Session/Chart migration。旧状态必须由部署操作清理后重新创建。

## 错误字段

三个 `Unsupported*SchemaError` 均包含：

```text
code: stable machine code
expected_version: int
actual_version: object | null
```

错误不携带原始 Session、checkpoint 或 Artifact 数据。

## 清理旧本地状态

先停止 Agent CLI/API，确认没有活跃 Run，再备份并删除对应 `ASSISTANT_AGENT_HOME/workspaces`。默认用户目录
通常为 `%USERPROFILE%\.assistant_agent\workspaces`。重新启动后创建新 Session。不要只删除单个 checkpoint
槽位或手工改 `schema_version`；这会破坏 Session/Run/Artifact 关联不变量。

示例（用户确认目标后执行）：

```powershell
$root = Join-Path $env:USERPROFILE '.assistant_agent\workspaces'
Copy-Item -LiteralPath $root -Destination "$root.m31-backup" -Recurse
Remove-Item -LiteralPath $root -Recurse
```

Agent 不会自动执行此删除，也不提供运行时旧数据迁移。

## 联调验收

1. API 固定 M31 commit 后可创建 Session、运行文本任务并读取 v3 snapshot。
2. `present_chart` 只返回 V2，API/Web 只走 V2 renderer。
3. 注入 v6 checkpoint、v2 Session、V1 Chart 时分别返回三个稳定 code，源文件字节不变。
4. 清空旧测试 workspaces 后，CLI/API 可从空状态正常启动。

