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

`create_output(filename, media_type, content, title?, disposition?)` 是 `safe_idempotent` 工具，CLI/Web
都注册。它只能写 OutputStore，不能指定路径。工具成功后 `ToolResult.output_artifact` 与
`StepEvent.output` 发布小型引用；内容不进入事件。RunState v9 持久 ToolResult 输出引用与 Run 输出列表，
恢复时按 `output_id` 去重。

CLI `/outputs` 列当前 Session 文件名、大小与本机路径；首版不提供单文件删除，避免历史消息引用
失效，Session 删除负责级联清理。API 使用
`SessionRuntime.list_outputs/get_output/get_output_payload`，不得读取 sidecar 或拼接路径。

## API handoff

API 必须精确固定 M33 Agent commit，并校验：Agent Service contract=4、Session contract=5、RunState=9、
Output contract=1、Event contract=1。建议网络端新增 Session Output 列表、元数据与认证内容下载接口；
WebSocket 只转发 `StepEvent.output` 引用，内容通过 REST 读取。HTTP 下载设置 Content-Type、
Content-Disposition、Content-Length、ETag 和 `X-Content-Type-Options: nosniff`。跨 Session访问统一 404。

Web Runtime 仍禁止 Shell/服务器文件工具，但允许 `create_output`；这不会赋予模型任意路径写权限。
HTML 不得注入页面 DOM。API/Web 不得复制 Output 存储、幂等、删除或恢复状态机。

## 验收

契约严格性、原子写与 hash、路径逃逸、MIME/容量上限、幂等冲突、Run v9 checkpoint、Session v5
同步、失败保留、删除级联、fork 深复制、Web allowlist、CLI 列表均有定向测试；Ruff 与 mypy 通过。
