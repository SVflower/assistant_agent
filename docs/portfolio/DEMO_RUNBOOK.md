# 项目展示 Demo Runbook

## 展示前准备

1. 准备一张真实但不含隐私的 UI 截图。
2. 准备一个本地 LM Studio 视觉模型和一个云端文本模型。
3. 准备一个简单的 `frontend-design` Skill 和一个可选的 MCP Server。
4. 删除演示环境中的历史测试 Session，避免左侧列表干扰。
5. 确认 API、Web 端口和临时 Bearer Token，不把 Token 放进录屏、仓库或命令历史。

## 建议演示顺序

### Demo A：模型能力边界

1. 连接云端 `deepseek-v4-flash-vision-exp`。
2. 上传截图并让 Agent 描述界面。
3. 展示右侧 Provider/Model、附件尺寸和 Thinking。
4. 切换到不支持视觉的模型，展示明确的 `unsupported_input_modality`。

面试表达：

> 能力不是由前端按钮猜出来的，而是由 Provider 配置和 Runtime capability 共同决定；未知能力默认拒绝，避免把图片静默丢给文本模型。

### Demo B：任务计划和交付物

输入一个四步骤 HTML 任务，展示计划更新、工具调用和输出文件。打开最终 HTML 时说明文件位于受管 outputs 根目录，并带有 session/run 归属，而不是散落到任意工作目录。

### Demo C：中断恢复

让 Agent 执行一个耗时任务，在工具执行或等待交互时暂停；检查器显示 paused，然后恢复或取消。展示刷新后从服务端快照恢复，而不是从浏览器内存“猜回去”。

## 录屏建议

- 录制 16:9，保留三栏工作台和右侧 Inspector。
- 第一版视频控制在 90 秒，重点展示一个完整闭环，不要把所有菜单都扫一遍。
- 开头 8 秒直接展示“上传截图 -> Agent 分析 -> 轨迹/结果”，不要从安装过程开始。
- 画面中保留模型名、Run 状态、耗时和文件交付位置。
- 故意展示一次暂停/恢复或不支持图片的安全失败，这比只展示成功更能体现工程能力。

## 录屏旁白模板

> 这是一个本地优先的 Agent Runtime，不是单纯聊天界面。用户上传图片后，Web 只提交附件引用，API 负责认证和事件传输，Agent Runtime 根据当前模型能力决定是否允许图片输入。运行过程中，用户能看到 Thinking、Task Plan 和工具轨迹；如果任务被暂停或进程中断，Run 可以从 checkpoint 恢复。最终 HTML、图表和输出文件由 Runtime 统一管理，历史消息和运行详情可以重新打开。

