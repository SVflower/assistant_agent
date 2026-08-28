# Assistant Agent 实现状态

本文面向维护者和集成方。用户安装入口请从仓库根目录 [README](../README.md) 开始。

## 已实现能力

- 云端/本地模型 provider 切换，统一 Agent Loop 和上下文预算；
- 内置文件、搜索、Shell、进程、图表、输出和附件工具；
- 统一权限、预算、审计、暂停/取消、进程清理和 checkpoint 恢复；
- Session/Run 持久化、ItemEvent、Interaction、TaskPlan、Observability 和安全 trajectory；
- MCP Server 接入、Skill 目录发现/按需加载、运行时能力自省和有界降级；
- ChartSpecV2、Managed Outputs、Mermaid、web_search/fetch_url；
- `assistant_agent.service`、`assistant_agent.contracts` 和 `assistant_agent.interaction` 公共入口。

## 设计边界

CLI、API 和 Web 是适配层，不复制 Agent 状态机、不解析日志或中文错误文本、不读取 Agent 私有磁盘结构。业务 MCP、企业知识库、MES/WMS/QMS/ERP 连接器和工业分析算法属于外部扩展。

Agent 会限制模型上下文和工具输出，不把大规模数据直接送入模型；大数据应通过 Artifact、分页、聚合或外部 Analysis Provider 处理。容器沙箱覆盖 Agent 内置 Shell/Git，外部进程由部署方负责隔离。

## 版本策略

当前采用 current-schema hard cut。旧 Session、Run checkpoint、Chart、Attachment 和 Output 格式不自动迁移，版本不匹配时 fail closed。精确版本以 `assistant_agent.service` 和 `assistant_agent.contracts` 代码为准；API 使用完整 commit SHA 固定 Agent 依赖。

## 未实现或有限能力

- 尚未发布 PyPI/pipx 正式发行版；
- 默认是单机/单用户，不包含多租户、公网部署、数据库后端或长期 TraceStore；
- 尚未提供完整企业权限、知识库服务、确定性 SPC/OEE 分析平台；
- trajectory、运行状态和输出受本地保留策略及容量上限约束；
- 外置 MCP、自定义 Python Tool 和 API/Web 进程不自动继承 Agent 容器隔离。

## 文档分层

- `README.md`：用户安装、核心能力、边界和开发入口；
- `docs/INSTALL.md`：平台安装和配置细节；
- `docs/ARCHITECTURE.md`：长期架构事实源；
- `docs/agent-service-integration-guide.md`：公共服务集成契约；
- `ROADMAP.md`、`docs/TECH_DEBT.md`：路线图和技术债；
- `docs/m*.md`、`docs/*handoff*.md`：阶段开发方案和跨项目交接；
- `docs/archive/`：历史迁移资料，不作为当前接口说明。

## 测试资产

`tests/` 和 `evals/` 是公开项目的一部分，用于复现契约、权限、工具生命周期、恢复和预算行为。本地真实 provider 测试需要用户自己的配置；不应提交 key、Session、Run、日志、输出、报告或缓存。
