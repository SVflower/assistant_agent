# M38 结构化运行项目与长任务编排方案

## 目标

在现有 AgentLoop、RunCoordinator、StepEvent、Session ledger 和 checkpoint 之上，补齐
Codex 风格的结构化运行项目模型，使长任务的计划、Thinking、工具、Artifact、输出和最终回答
都能被稳定关联、增量更新、重连重放和历史恢复。

## 兼容原则

- 现有 `EVENT_CONTRACT_VERSION=1`、Session/RunState/Output/Chart 契约继续有效。
- 新字段先全部 additive，旧 CLI/API/Web 忽略即可继续运行。
- Agent 仍是 Run、Session、权限、恢复、Artifact 和 Output 的唯一 owner。
- API 不复制 Agent 状态机，Web 不从文本猜测状态。
- 任何新状态必须在既有 checkpoint 边界持久化，不能建立第二套恢复状态。

## 已实施的第一步

`StepEvent.item_id` 已作为可选字段加入：

- 工具调用和工具结果使用同一个 `item_tool_<call_id>`；
- 最终回答使用 `item_final`；
- 旧调用方不读取该字段时行为不变。

这一步先解决结构化更新所需的稳定身份，不改变事件顺序、工具执行、权限和终态语义。

## 下一步实施顺序

### M38-A：RunItem 生命周期

新增受控 Item 类型：`user`、`plan`、`reasoning`、`tool`、`artifact`、`output`、`final`、
`compaction`、`terminal`。每个 Item 使用 `planned/started/streaming/waiting/completed/failed/
cancelled` 生命周期，状态由 RunCoordinator 管理并随既有 checkpoint 保存。

### M38-B：上下文编排器

将模型上下文分成当前任务、当前阶段、最近相关消息、压缩摘要和 Artifact 引用。大型工具结果
只保留有界摘要/头尾/ref，公开历史继续保真。模型每轮看到的工具 Schema 也按剩余预算计算。

### M38-C：阶段化长任务

为 planning、analysis、implementation、verification、repair、delivery 提供阶段状态、完成条件、
当前阶段 checkpoint 和阶段内重试。已完成副作用不重放，失败只重试当前未完成阶段。

### M38-D：API/Web 投影

API 增加可选 Item DTO 与事件投影；Web 以 `run_id + seq` 去重、以 `item_id` upsert：

- 会话正文展示用户结果；
- 输入框上方展示可折叠 TaskPlan；
- 当前回答中展示可折叠 Thinking；
- 轨迹展示时间轴；
- Inspector 展示所选 Item 详情。

## 不变量与验证

- 一个 Run 只有一个 terminal；
- 一个 Item 只能完成一次；
- 重连不创建新 Run；
- 重复 seq/item_id 不产生重复消息；
- 取消后不能变成 completed；
- 已完成工具不重放；
- Artifact/Output 只发布一次；
- 旧 StepEvent 顺序和现有 CLI/API/Web 行为保持不变。

每一小步只运行相关单测、契约测试和受影响的 API/Web 测试；不以全量测试作为本阶段前置条件。
