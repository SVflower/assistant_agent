# M33 Managed Outputs

## 范围与原则

M33 为 CLI 与 Web Runtime 提供统一、可配置、可恢复的用户交付文件能力。源码改动、内部 Tool
Artifact、输入附件、Chart Presentation 与 Deliverable Output 是不同事实；`write_file` 的结果不会自动
成为交付物。Agent 是 Output 元数据、内容、归属和生命周期的唯一 owner，API 不扫描服务器目录。

本里程碑采用开发期 hard cut：RunState v9、Session schema/contract v5、Agent Service contract v4，
不读取旧状态。Event contract 保持 v1，`StepEvent.output` 是 additive 字段。不修改 `agent/loop.py`。

## 公共契约

`OutputArtifactV1` 包含 `schema_version/output_id/session_id/run_id/message_id/call_id/filename/`
`media_type/size_bytes/content_hash/created_at/disposition/preview_supported`。公共 DTO 不包含 path、root、
环境变量、命令或原始异常。`OutputPayload` 是 Artifact 与 UTF-8 内容；首版仅允许受限文本生产者。

稳定错误码：`output_invalid`、`output_limit_exceeded`、`output_not_found`、`output_conflict`、
`output_unavailable`。API 必须按错误类型映射，不解析中文文本。

## 路径与配置

默认根为可信 workspace 下的 `outputs`，默认布局：

```text
<workspace>/outputs/YYYY/MM/DD/<session-id>/<opaque-output-id>--<filename>
<workspace>/outputs/YYYY/MM/DD/<session-id>/<opaque-output-id>.json
```

`outputs.root` 可由可信部署配置设为绝对路径；模型和 Web 请求不能指定 root。CLI 可通过本地 Store
显示实际路径，公共契约始终不暴露路径。支持 `flat/date/date_session` 布局，默认 `date_session`。

首版 MIME 白名单：`text/html`、`text/markdown`、`text/csv`、`application/json`、`text/plain`。
HTML 仅作为数据保存，CLI 不执行，未来 Web 只能在严格 sandbox iframe 中预览。二进制图片/PDF 由未来
可信 producer port 扩展，不允许模型提交任意 base64。

## 存储与生命周期

- Store 先原子写 payload，再原子写 metadata；读取校验大小与 SHA-256。
- `output_id` 由 Run ID + call ID 确定性生成；同 ID 同载荷幂等，同 ID 不同载荷冲突。
- basename、控制字符、Session 隔离和 root containment 全部 fail closed。
- 单文件、每 Run 文件/字节、每 Session 文件/字节均有硬限。
- 正式 Output 不受 Tool Artifact prune 影响；Run 失败不删除已经发布的 Output。
- terminal sync 将 Run Output refs 幂等写入 Session；Session 删除级联删除。
- Session fork 深复制 payload，生成目标 Session 自己的 opaque ID，不共享可变路径。

## 工具、恢复与展示

`create_output(filename, media_type, content, title?, disposition?)` 仅用于不超过配置分块上限的短文本。
长 HTML/Markdown/CSV/JSON/文本使用 `manage_output` 的 `begin -> append -> finalize` 动作；默认每块最多
8192 UTF-8 bytes，按连续 `chunk_index` 幂等追加，块数、单文件、Run 与 Session 总字节均有硬限。
这组工具是 `safe_idempotent`，CLI/Web 都注册，只能写 OutputStore，不能指定路径。成功后
`ToolResult.output_artifact` 与
`StepEvent.output` 发布小型引用；内容不进入事件。RunState v9 持久 ToolResult 输出引用与 Run 输出列表，
恢复时按 `output_id` 去重。

草稿按 Session/Run/draft 隔离，metadata、每块及 finalize 完成标记均原子写入。暂停或进程中断保留草稿，
恢复可继续且 finalize 重放返回同一 Artifact；completed/failed/cancelled 终态和 Session 删除负责清理未完成
草稿。相同工具参数解析/Schema 错误连续两次后由 Registry 执行层熔断：`create_output` 引导改用分段协议，
分段工具族熔断后要求降级为简短文字，不能依靠模型自觉停止。

CLI `/outputs` 列当前 Session 文件名、大小与本机路径；首版不提供单文件删除，避免历史消息引用
失效，Session 删除负责级联清理。API 使用
`SessionRuntime.list_outputs/get_output/get_output_payload`，不得读取 sidecar 或拼接路径。

## API handoff

API 必须精确固定 M33 Agent commit，并校验：Agent Service contract=4、Session contract=5、RunState=9、
Output contract=1、Event contract=1。建议网络端新增 Session Output 列表、元数据与认证内容下载接口；
WebSocket 只转发 `StepEvent.output` 引用，内容通过 REST 读取。HTTP 下载设置 Content-Type、
Content-Disposition、Content-Length、ETag 和 `X-Content-Type-Options: nosniff`。跨 Session访问统一 404。

Web Runtime 仍禁止 Shell/服务器文件工具，但允许完整受管输出工具族；这不会赋予模型任意路径写权限。
HTML 不得注入页面 DOM。API/Web 不得复制 Output 存储、幂等、删除或恢复状态机。

本次长输出稳健性修复不改变 `OutputArtifactV1`、StepEvent、RunState、Session 或公共 Service 接口，
因此 Agent Service contract 仍为 v4。API 必须将新增的安全工具 `manage_output` 纳入 Web 部署 allowlist；
Web 网络 DTO 无变化，仍只会收到最终 `assistant.output`。

## 验收

契约严格性、原子写与 hash、路径逃逸、MIME/容量上限、幂等冲突、Run v9 checkpoint、Session v5
同步、失败保留、草稿分块/恢复/清理、参数熔断、删除级联、fork 深复制、Web allowlist、CLI 列表均有
定向测试；Ruff 与 mypy 通过。未修改 `agent/loop.py`。
