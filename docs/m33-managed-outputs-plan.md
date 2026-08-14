# M33 Managed Outputs

## 范围与原则

M33 为 CLI 与 Web Runtime 提供统一、可配置、可恢复的用户交付文件能力。源码改动、内部 Tool
Artifact、输入附件、Chart Presentation 与 Deliverable Output 是不同事实；`write_file` 的结果不会自动
成为交付物。Agent 是 Output 元数据、内容、归属和生命周期的唯一 owner，API 不扫描服务器目录。

本里程碑最终采用开发期 hard cut：RunState v10、Session schema/contract v5、Agent Service contract v5，
不读取旧状态。Event contract 保持 v1，`StepEvent.output` 字段不变。本次经用户批准修改 `agent/loop.py`。

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

模型只看到 `create_output(filename, media_type, title?, disposition?)`，正文不进入工具 JSON。
工具成功后 RunState v10 保存私有 pending capture；下一模型轮 tools 为空，普通文本流由 Runtime 自动按
8192 UTF-8 bytes 上限分块写入并 finalize。草稿 ID、块序号和 finalize 均不进入模型或 API。
捕获正文不发布 content_delta。成功后使用原 create_output call_id 发布带 OutputArtifactV1 的 tool_result。

草稿按 Session/Run 隔离。暂停或进程中断后保留意图，但恢复会先删除半文件并从头捕获；确定性
Run/call ID 保证只发布一个 Output。completed/failed/cancelled 与 Session 删除清理未完成草稿。
`create_output` 参数连续错误仍由 Registry 两次熔断。

CLI `/outputs` 列当前 Session 文件名、大小与本机路径；首版不提供单文件删除，避免历史消息引用
失效，Session 删除负责级联清理。API 使用
`SessionRuntime.list_outputs/get_output/get_output_payload`，不得读取 sidecar 或拼接路径。

## API handoff

API 必须精确固定 M33 Agent commit，并校验：Agent Service contract=5、Session contract=5、RunState=10、
Output contract=1、Event contract=1。建议网络端新增 Session Output 列表、元数据与认证内容下载接口；
WebSocket 只转发 `StepEvent.output` 引用，内容通过 REST 读取。HTTP 下载设置 Content-Type、
Content-Disposition、Content-Length、ETag 和 `X-Content-Type-Options: nosniff`。跨 Session访问统一 404。

Web Runtime 仍禁止 Shell/服务器文件工具，只允许元数据型 `create_output`；这不会赋予任意路径写权限。
HTML 不得注入页面 DOM。API/Web 不得复制 Output 存储、幂等、删除或恢复状态机。

API 删除对旧 `manage_output` capability 的假设，不管理草稿。Web 网络 DTO 无变化，仍只收到最终
`assistant.output`。Output v1、Session v5、Event v1 不变。

## 验收

契约严格性、原子写与 hash、路径逃逸、MIME/容量上限、Run v10 checkpoint、Session v5 同步、
捕获不发 delta、恢复重启、失败无半文件、删除级联、fork、Web allowlist 均由定向测试覆盖。
