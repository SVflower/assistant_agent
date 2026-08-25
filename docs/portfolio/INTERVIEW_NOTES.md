# 面试讲解提纲

## 30 秒版本

我做的是一个本地优先的多模态 Agent 平台，核心不是聊天，而是可靠地执行长任务。它把模型、工具、MCP、Skill、附件、权限、checkpoint、REST/WebSocket 和 Web 可观测工作台串成完整 Run 生命周期。重点解决了模型切换、工具副作用、任务中断、断线重连、重复事件和结果交付这些真实工程问题。

## 为什么拆成三个仓库

- Agent Core 拥有模型调用、工具循环、Session/Run、权限、恢复和持久化。
- API 是无 UI 的协议适配层，负责鉴权、线程桥、REST、WebSocket、事件重放和生命周期调度。
- Web 只负责展示和交互，不持有 Provider 密钥，不直接执行 Shell，不复制 Agent 状态机。

拆分的收益是 CLI、Web 和未来其他调用方共享同一套 Runtime 语义，同时可以独立测试协议边界。

## 最值得讲的三个难点

### 1. 长任务恢复

每次 Run 有独立 checkpoint，终态和副作用边界被持久化。恢复时不会盲目重放已确认工具；副作用开始但结果未确认时进入不确定状态，由用户选择 retry/skip/abort。

### 2. 事件一致性

WebSocket 事件按 `run_id + seq` 去重，Session 消息使用稳定 ID，终端事件唯一。断线时从服务端 snapshot 和 `after_seq` 恢复，不靠前端 append，也不重复启动 Run。

### 3. 模型能力与附件

附件先保存为受控引用，Provider boundary 再根据 capability materialize 成模型所需的内容块。文本模型收到图片时 fail closed；Vision 模型才生成 `image_url`，同时受图片大小、像素和上下文预算约束。

## 常见追问的回答

### 为什么不让模型直接执行 Shell？

因为模型输出是不可信输入。工具注册、参数 schema、权限门、路径边界、超时、输出预算和进程树清理必须在 Runtime 侧执行，不能交给 Prompt 自律。

### 为什么需要 REST 和 WebSocket 两套协议？

REST 适合创建、查询、控制和下载；WebSocket 只负责有序增量事件。断线后 REST snapshot 是权威基线，WebSocket 用 sequence 做有限续播。

### 为什么不把所有过程都塞进对话？

对话是用户结果，轨迹是工程事实。把工具参数、耗时、重试和 checkpoint 全塞进消息会污染上下文，也会让历史恢复和权限边界变得模糊。

### 当前最大不足是什么？

当前仍是本地优先单机平台，缺少公网多租户、数据库化 TraceStore、分布式队列、企业身份系统和完整工业分析引擎。我会先补发布体验和可复现 Demo，再按真实用户场景推进这些能力。

## 适合投递的岗位方向

1. AI Agent / LLM Application Engineer
2. Agent Platform / AI Infrastructure Engineer
3. Full-stack AI Engineer
4. Developer Tools / Coding Agent Engineer
5. 本地化 AI、模型接入和企业智能应用工程师

