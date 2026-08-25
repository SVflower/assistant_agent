# Assistant Agent 项目展示

## 一句话定位

Assistant Agent 是一个本地优先的、多模态、可恢复、可观测任务 Agent 平台：它把云端和本地模型、受控工具、MCP、Skill、附件、图表、文件交付和 Web 工作台放进同一套可验证的 Run 生命周期中。

它不是“给模型套一个聊天窗口”，而是解决 Agent 在真实任务中最难的工程问题：长任务如何不中断、工具失败如何恢复、模型切换如何保持一致、用户如何知道系统正在做什么、以及结果如何被审计和复现。

## 面试官应该看到什么

| 维度 | 项目证据 |
| --- | --- |
| Agent Runtime | 多轮任务、工具循环、预算、权限、checkpoint、暂停/取消/恢复 |
| 模型工程 | OpenAI 兼容、Anthropic、本地 LM Studio/Ollama/vLLM；仅改配置切换 |
| 多模态 | 图片/文本附件上传、能力声明、token 预算、模型不支持时 fail closed |
| 平台工程 | Agent Core、API、Web 三仓库分层，REST + WebSocket，契约版本化 |
| 可观测性 | Run 状态、阶段耗时、token/context、Thinking、轨迹时间账本、Task Plan |
| 安全 | Web Runtime allowlist、MCP/Skill 信任边界、SSRF 防护、路径和进程隔离 |
| 交付能力 | 受管 HTML/Markdown/CSV/JSON 输出、图表 Artifact、历史和断线恢复 |
| 质量 | 定向契约测试、真实浏览器联调、架构 import-linter、类型检查和 CI |

## 推荐项目名称

简历标题建议使用：

> **Assistant Agent Runtime & Observability Workbench**

中文副标题：

> 面向真实任务执行的本地优先、多模态 Agent Runtime 与可观测 Web 工作台

不要使用“AI 聊天机器人”“智能问答系统”作为主标题。这些词会掩盖项目最有价值的运行时和平台工程能力。

## 5 分钟演示路径

### 0:00 - 0:40，展示架构

打开三仓库架构图，说明依赖方向：

```text
Web Workbench -> API Adapter -> Agent Service -> Agent Runtime
                                      |
                       Model / Tools / MCP / Skills / Persistence
```

强调 Web 不持有 Provider 密钥，也不直接访问文件系统或 Shell；API 只做协议和生命周期桥接；Agent Core 拥有 Run、Session、权限和恢复语义。

### 0:40 - 1:40，多模态任务

在 Web 上传一张截图，提问“分析这个界面并给出改进建议”。展示：

1. 附件摘要、尺寸和 token 估算；
2. Agent 根据模型能力决定是否允许图片输入；
3. Thinking 与最终回答分开；
4. Run 状态和实时耗时；
5. 完成后历史仍可恢复。

切换到不支持视觉的模型，展示系统拒绝图片并返回结构化原因，而不是让模型收到无效输入后产生幻觉。

### 1:40 - 2:40，真实交付任务

输入：

> 分步骤完成一个后台管理页面：分析页面结构、生成完整 HTML、检查布局和交互、发布最终文件，执行过程中维护任务计划。

展示 Task Plan、工具轨迹、受管输出目录、HTML 预览和最终文件引用。说明输出正文由 Runtime 捕获并管理，模型不能伪造一个“文件已经生成”的链接。

### 2:40 - 3:30，可靠性控制

在任务执行中点击暂停，再恢复；或模拟工具等待时取消任务。展示：

- 唯一 `run.terminal`；
- 取消不会让页面永久转圈；
- Run 可从 checkpoint 恢复；
- 工具副作用未确认时不会假装成功。

### 3:30 - 4:20，轨迹与检查器

切换“会话 / 轨迹”视图，点击某一条工具记录，展示右侧 Inspector 的 Run、阶段、耗时、上下文和 token 信息。解释：对话是用户结果视图，轨迹是工程诊断视图，两者不重复承担职责。

### 4:20 - 5:00，开发者视角

展示一个测试或契约：

- WebSocket 按 `run_id + seq` 去重；
- Session/Run 使用稳定 ID；
- 图片附件进入历史时深层 Vue Proxy 被安全解代理；
- API 不解析 Agent 内部文件，也不复制状态机。

最后用一句话收束：

> 我关注的不是让模型“看起来会做事”，而是让每一步都可观察、可恢复、可限制、可验证。

## 面试版项目描述

### 中文

独立设计并实现本地优先的多模态 Agent Runtime 及 Web 工作台，拆分为 Agent Core、FastAPI/WebSocket Adapter 和 Vue 3 前端三个仓库。实现云端与 LM Studio 等本地模型切换、图片/文本附件、Skill/MCP 扩展、受控工具执行、Session/Run checkpoint、暂停/取消/恢复、结构化错误、实时轨迹、Thinking、Task Plan、图表 Artifact 和受管文件输出。通过版本化 REST/WebSocket 契约、Web Runtime allowlist、预算与权限门、幂等事件和定向真实浏览器联调，解决长任务中断、重复消息、工具失控、模型能力不匹配和历史不可恢复等问题。

### English

Designed and built a local-first multimodal agent runtime with a Vue 3 workbench and a FastAPI/WebSocket adapter. The platform supports cloud and local OpenAI-compatible providers, image/text attachments, Skills, MCP tools, governed tool execution, resumable Session/Run checkpoints, pause/cancel/resume, structured failures, live trajectory, reasoning presentation, task plans, chart artifacts, and managed file outputs. Versioned REST/WebSocket contracts, runtime tool allowlists, budgets, idempotent event handling, and real-browser integration tests make long-running agent tasks observable, recoverable, and auditable instead of treating the model as an opaque chat endpoint.

## 不要在面试中夸大的内容

- 不要说“支持所有模型”；应说“通过 Provider Adapter 支持已验证的云端和本地 OpenAI-compatible/Anthropic 后端”。
- 不要说“生产级多租户 SaaS”；当前定位是本地优先单机/本机服务，尚未包含公网部署、多用户和数据库集群。
- 不要说“完整 TraceStore”；当前是有界 Run trajectory 和历史保留范围。
- 不要说“模型自己保证安全”；安全边界由 Runtime Policy、allowlist、权限门和 API/Web profile 强制执行。
- 不要把图表说成工业分析平台；当前有受控普通图表和数据契约，SPC 等确定性工业算法仍属于后续扩展。

## 代码入口

- Agent Core：`src/assistant_agent/application/`、`src/assistant_agent/runtime/`、`src/assistant_agent/contracts/`
- 工具与扩展：`src/assistant_agent/tools/`、`src/assistant_agent/integrations/`
- API：`assistant_agent_api/src/assistant_agent_api/`
- Web：`assistant_agent_web/src/`
- Agent 架构说明：`docs/ARCHITECTURE.md`
- 当前里程碑：`ROADMAP.md`
- API 服务边界：`assistant_agent_api/README.md`
- Web 运行与质量门：`assistant_agent_web/README.md`

