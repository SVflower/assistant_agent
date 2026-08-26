# M32 Agent -> API 交接

Agent M32 本地提交已完成。公共服务契约升为 **v3**，Event contract 仍为 **v1**。

## API 必改

1. 清理旧 Session v3 / RunState v7 的测试状态；Agent 当前只接受 Session v4、RunState v8。
2. 上传端只把字节、显示名和可选 MIME 传给 `SessionRuntime.ingest_attachments()`；不得让 Web/API
   选择服务器路径、写入路径或构造 AttachmentRef。
3. 把 `AttachmentSummaryV1` 返回给 Web。创建 Run 时发送 `UserMessageInputV1`，用户内容为
   `MessageContentV1(parts=[TextPartV1 | AttachmentPartV1])`。纯文本仍可传字符串。
4. 用 `RuntimeCapabilities.content_parts_version == 1`、`input_modalities`、
   `attachment_media_types`、`attachment_limits` 渲染上传能力；Web 不自行估算图片 token。
5. 把 `attachment_too_large`、`attachment_context_too_large`、`unsupported_input_modality`、
   `attachment_invalid`、`attachment_unavailable` 映射为安全用户提示，不回显原始文件内容或路径。

## 语义

- `ingest_attachments` 批量原子：任一附件无效则不发布部分引用。
- 未绑定附件可调用 `delete_unbound_attachments(ids)`；存储也按 TTL 回收。
- 成功 `start_run` 将引用绑定到 Session。Session 删除级联；Run 删除不级联已绑定附件。
- provider 只在调用边界临时得到 data URL（图片）或边界文本（文本）；ItemEvent 不新增附件 payload。
- API/Web 不需要升级 EventKind 或解析终端日志。
