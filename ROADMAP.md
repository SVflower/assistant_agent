# ROADMAP — 后续路线图

> 开工蓝本。第一阶段（MVP）已完成，见 [DESIGN.md](DESIGN.md) 第 8 节。
> 本文档规划第二阶段起的里程碑，每个里程碑只列可清晰验收的目标。
> 最后更新：2026-07-19

---

## 项目当前状态（截至 2026-07-19）

**一句话**：从"能跑的 MVP"长成了一个功能相当完整、多平台实测、可观测且运行时预算可控、全程守调研→方案→测试→验收纪律的本地 Agent。

**已具备能力**：
- **模型**：后端可切换（云端 OpenAI 兼容 / Anthropic / 本地 LM Studio·Ollama·vLLM），config/`--provider`/对话内 `/model` 三种切法，切换保留上下文。
- **交互**：15 FPS 流式 Markdown + 语义工具摘要 + normal/verbose/quiet + `/display`/`run --quiet`；normal 过程文本只在活动区显示、最终回答才落屏；Write/Edit 权限前有界代码预览/结构化 diff，代码底板与增删行背景明确分区；全宽输入边界与会话启停 ID；思考状态、耗时/token、上下文占用与 Ctrl+C 中断。
- **安全/控制**：Registry 强制统一权限门（deny→ask→allow）；四种权限模式与独立 capability；第一次 Ctrl+C 可恢复暂停、第二次强制取消；Windows Job Object/POSIX process group 清理受管进程树；Host/Confined/Container 三种 Workspace，其中容器默认无网络、非 root 且受资源限制。
- **记忆/恢复**：token 感知截断与摘要压缩；Session JSON 持久化；步骤级 Run checkpoint、
  `runs`/`resume`、双槽损坏回退和副作用不确定状态人工处置。
- **公共服务边界（M16）**：UI 无关 Runtime 工厂、同步 InteractionPort、隔离 SessionRuntime、
  Session/Run 门面和版本化 StepEvent；CLI/API 复用同一装配与恢复语义。
- **生产 Runtime 策略（M17）**：调用方不可绕过的 RuntimePolicy；MCP optional/required、连接与调用
  timeout 分离及有界并行启动；Tool/Skill/MCP/sandbox 脱敏能力快照和一次性探测。
- **运行可解释性（M18）**：结构化 RunFailure、activity 与安全预算快照；iteration/tool call/tool
  output 三类 continuation 统一走 InteractionPort，扩展只作用当前 Run 并进入 checkpoint；Provider、
  工具、权限和依赖错误提供稳定机器分类，failed run_terminal 不再依赖文本推断。
- **架构重建（M19）**：contracts 单一所有权、Agent Context/Run 内聚、Application 用例、Bootstrap
  唯一装配和 Service 稳定转发；execution/persistence/observability/integrations 适配器归位，
  依赖方向由 12 条 import-linter contract 强制。
- **工具**：读/写/局部编辑/列目录/shell/代码检索/git 只读/用户澄清，以及带来源的
  `web_search`/`fetch_url`；搜索 backend 可替换，抓取含 SSRF、重定向和响应上限防护。
- **命令层**：slash 命令系统本地拦截不花 token；`/skills` 与 `/mcp` 支持列出、安装、诊断、
  启停、信任和安全卸载，配置变更明确在下次启动生效。
- **上手**：`assistant-agent init` 交互向导 + `docs/INSTALL.md` 多平台安装（Windows/WSL2 实测）。
- **健壮性**：对"笨模型"容错、Windows/Linux 终端适配、保存不崩、自动保存非致命。
- **可观测（M6）**：结构化 JSONL 事件日志（工具调用/耗时/成败/授权决策留痕）+ 尽力脱敏 + 禁用零副作用。
- **运行时预算（M6.5）**：任务级工具调用总数 + 单次/累计工具输出上限；预算耗尽时补齐当前批次结果再安全终止，不留悬空 tool call。
- **技能（M7a/M11c）**：SKILL.md 发现 + 渐进披露；project 使用 `.agents/skills/`，user 使用
  `~/.assistant_agent/skills/`，旧项目目录只读兼容；`/skills` 支持受管安装/卸载，脚本仍走既有权限门。
- **MCP（M7b/M7c）**：MCP client（stdio + HTTP 两种 transport）——外部 server 工具接入，命名空间 `mcp__<server>__<tool>`；同步桥（守护线程常驻 loop + run_coroutine_threadsafe）；每工具主动确认（category 按 server+tool 细分）；工具白/黑名单 + 每 server/全局数量上限防 schema 撑爆；HTTP 走 Streamable HTTP，session/协议头/重连交 SDK 代管、调用层不自动重放；`/mcp` 命令；`cli/setup.py` Runtime 统管生命周期（还清 D7）。
- **上下文进化（M8a/M8b）**：M8a 统一预算口径——可用消息预算 = 窗口 − system − tools schema − reserved_output，`/context` 分项显示真实占用（还 D10）；M8b 摘要压缩替代硬截断——双历史（raw + checkpoint + tail）、按完整用户轮分组、checkpoint 随 Session 持久化（resume 不重复摘要）、摘要 token 独立计入 usage、摘要失败降级硬截断，默认关闭时上下文逐字节等于现状。
- **行为评测（M9c/M10b）**：scripted/real 双轨 eval；18 个 scripted 案例覆盖轨迹、权限、预算、终止、压缩和文件副作用，另有 4 个真实故障注入 recovery eval；真实 provider 报告支持重复运行与 A/B compare。
- **工具契约（M10a）**：统一 JSON Schema 运行时校验与结构化 ToolResult；10 万行范围读取、流式上下文搜索、目录上限；Shell/Git 双流有界捕获与受限 Artifact；文件原子写；MCP structuredContent 保真。
- **可恢复执行（M10b）**：Conversation/Session/RunState 分离；模型、审批、副作用前后 checkpoint；
  已完成工具不重放，started 副作用需 retry/skip/abort；预算、重复熔断、权限和摘要状态跨进程恢复；
  trace/session/run/call 标识对齐，还清 D8。

**质量**：606 测试通过（5 个平台能力测试跳过）、覆盖率 84%、13974 行生产 Python 源码 +
1366 行 eval 基础设施，Ruff/mypy 全绿。架构适应度测试（声明式依赖契约 + 600 行非阻断评审）、技术债册、
DoD 和里程碑工作流全在；CI 已加入 format/lint/mypy/coverage/scripted eval/recovery eval 与
Windows/Linux、Python 3.11/3.13 矩阵。

**边界（明确未做）**：外置 MCP/自定义 Python Tool 的容器化、远程 Workspace、子 Agent 编排、
Web GUI、rewind/recap、非交互 init、PyPI 分发。

**阶段状态**：第一至第十二阶段已完成。M10c 的
“不做全栈 async”决策保持不变；M14 以同步 RunControl、跨平台 ProcessSupervisor 和 Workspace
抽象补齐受控执行边界并还清 D18。

**当前进展**：M19 已完成。公共调用继续只依赖 `assistant_agent.service` / `contracts` / `interaction`，
StepEvent v1 与 checkpoint v3 不变；内部目录迁移不要求 API 穿透实现。见
[架构事实源](docs/ARCHITECTURE.md)与[正式服务契约](docs/agent-service-integration-guide.md)。

**剩余技术债**：5 项（D5/D6/D12/D20/D21）。M9a 已还清 D13/D15/D17，M9b 已还清
D14，M9c 已还清 D9，M10a 已还清 D16，M10b 已还清 D8，M11a 已还清 D19，M14a 已还清 D18。详见
[技术债登记册](docs/TECH_DEBT.md)。

---

## 第一阶段回顾（已完成 ✅）

**能力**：模型后端可切换（云端/本地）、ReAct 多轮工具调用、四工具（读/写/列/shell）、
环境感知提示词、CLI + 终端 UI。28 测试绿、私有仓库备份。

**当前边界**：无跨会话记忆、非流式（黑屏等待）、无初始化向导、单 Agent、非真沙箱、工具集窄。

**已查清的关键事实**：
- ✅ **同会话（短期）记忆已存在**：`chat` 复用同一个 `AgentLoop`/`Conversation`，`run` 追加不重置，连续对话能记得前文。
- ❌ **跨会话（中期）记忆缺失**：`chat` 退出后内存中的对话即丢失，下次重启从零开始。← 里程碑 3 的真正缺口。

**技术债**：上下文截断是"消息数量"而非"token"截断（本地小模型可能仍超窗）；本地模型工具调用不稳；无结构化日志。

---

## 里程碑总览

### 第一阶段（全部完成 ✅）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| MVP | 配置/模型抽象/工具/ReAct/CLI | ✅ |
| M2 | 流式与过程透明 | ✅ |
| M2.5 | 确认机制升级 + 两层用户选择 + Ctrl+C 中断 | ✅（拒绝附原因回传待做）|
| 质量护栏 | 架构适应度测试 + 技术债册 + 覆盖率 + DoD + 里程碑工作流 | ✅ |
| M3 | 记忆与会话持久化（token 截断 + JSON 存档）| ✅ |
| M4 | 工具集扩展（code_search + git 只读）| ✅ |
| M4.5 | 模型管理与切换（--provider / /model / providers）| ✅ |
| M4.7 | 循环工程与写入安全（工作区范围 + 重复熔断 + 用尽优雅）| ✅ |
| M4.8 | 基础工具补全（edit_file + multi_edit）| ✅ |
| M4.9 | Slash 命令系统 | ✅ |
| M5 | 上手体验（init 向导 + INSTALL 多平台）| ✅ |

### 第二阶段（已完成 ✅）

> 总体目标：从"功能完整的单体 agent"走向"**可观测、可扩展、可接生态**的平台"，每步小切口、优先不动内核。
> 方向评估与路线见 [M6 归档方案](docs/archive/phase2/m6-observability-plan.md)（含第二阶段启动评审结论）。
> M6→M8b 全部交付，各里程碑计划均已归档至 `docs/archive/phase2/`。下一阶段方向待定。

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M6 | 结构化日志与工具审计（obs 层：JSONL 事件 + 权限决策留痕）| ✅ |
| M6.5 | 运行时预算与工具协议完整性（单次/累计输出 + 工具调用总数）| ✅ |
| M7a | Agent Skills 系统（SKILL.md 发现 + 渐进披露 + load_skill）| ✅ |
| M7b | MCP client（stdio + 同步桥 + 命名空间 + 权限/过滤/Runtime）| ✅ · [方案](docs/archive/phase2/m7b-mcp-plan.md) |
| M8a | 上下文预算口径（计入 tools schema + reserved，还 D10）| ✅ · [方案](docs/archive/phase2/m8a-context-budget-plan.md) |
| M8b | 上下文进化（摘要压缩 + 双历史模型）| ✅ · [方案](docs/archive/phase2/m8b-context-compaction-plan.md) |
| M7c | MCP Streamable HTTP transport（重连不重放）| ✅ · [方案](docs/archive/phase2/m7c-mcp-http-plan.md) |

> **M7 系列已收全（a/b/c）**：技能 + MCP（stdio + HTTP）。评审的 3 阻断 + 4 设计问题全部落地：
> MCP 工具主动确认（category 按 server+tool）、工具过滤/schema 上限防撑爆、HTTP 重连不重放
> （委托 SDK 管 session/协议头，调用层不自动重试）、M7b 直接还 D7。
> **M8 系列已收全（a/b）**：M8a 统一预算口径（还 D10），M8b 摘要压缩替代硬截断（双历史 +
> checkpoint 持久化 + 按轮分组 + 降级兜底，默认关闭时逐字节等于现状）。第二阶段里程碑全部完成。

### 第三阶段（已完成 ✅）

> 总体目标：从“功能完整的单体 Agent”进入“边界明确、失败可恢复、行为可评测”的可信执行阶段。
> 唯一总规划见 [第三阶段规划](docs/archive/phase3/phase3-trustworthy-agent-plan.md)。每个子里程碑开工前仍需细化方案；
> M10b 涉及 `agent/loop.py` 的实质改动，已在用户单独确认后实施并通过全量回归。

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M9a | 硬正确性与工程基线 | ✅ · [方案](docs/archive/phase3/m9a-hard-correctness-plan.md) |
| M9b | 统一权限与信任边界 | ✅ · [方案](docs/archive/phase3/m9b-permission-boundary-plan.md) |
| M9c | Agent 行为 Eval 与 CI 质量闭环 | ✅ · [方案](docs/archive/phase3/m9c-agent-evals-plan.md) |
| M10a | 工具契约与大文件/大输出工程 | ✅ · [方案](docs/archive/phase3/m10a-tool-contract-plan.md) |
| M10b | 步骤级 Checkpoint 与可恢复执行 | ✅ · [方案](docs/archive/phase3/m10b-recoverable-execution-plan.md) |
| M10c | 异步与可取消运行时 | ✅ 暂不全栈 async · [决策](docs/archive/phase3/m10c-async-runtime-decision.md) |

> **M9c 已完成**：新增顶层 `evals/`，scripted/real 双轨 runner、版本化 YAML schema、
> fixture confinement、可解释 scorer、JSONL/Markdown 报告与 A/B compare；14 个 deterministic
> 案例全绿，5 个 real-tag 案例可调用任意现有 provider。外部 Skills/MCP 默认关闭并需显式启用；
> CI 不调用真实模型。全量 303 passed、1 skipped、覆盖率 74%，未修改 `agent/loop.py`，还清 D9。

> **M10a 已完成**：ToolResult 增加稳定 code/retryable/metadata/Artifact；Registry 在权限与副作用前
> 执行 Draft 2020-12 参数校验；文件工具支持 10 万行分页、流式搜索、原子写和换行保持；Shell/Git
> 使用双流有界捕获与 workspace Artifact；MCP structuredContent 不再丢失。18 个 deterministic
> eval 全绿，全量 335 passed、2 skipped、覆盖率 76%，未修改 `agent/loop.py`，还清 D16。

> **M10b 已完成**：严格 RunState 与双槽 RunStore；模型/审批/工具前后 checkpoint；稳定 call ID；
> planned、部分批次和 started 不确定状态恢复；预算/重复熔断/精确授权/摘要 checkpoint 跨进程保持；
> `runs`/`resume` CLI 与 terminal Session 幂等同步；日志 trace/session/run/call/provider/model 对齐。
> 18/18 scripted eval + 4/4 recovery fault-injection eval 全绿，全量 392 passed、2 skipped、覆盖率 78%。
> 经用户单独授权修改 `agent/loop.py`，原有测试无回退；还清 D8。

> **M10c 决策完成**：当前证据不足以支持全栈 async 重构；协程取消也不能停止线程池中的同步
> 工具。D18 保留，推荐先独立实现并跨平台故障测试 Windows Job Object / POSIX process group
> 监管；async 核心、同步 facade 与只读工具并行仅在量化触发条件成立后立项。

### 第四阶段（已完成 ✅）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M11a | CLI 对话展示重构 | ✅ · [方案](docs/archive/phase4/m11a-cli-conversation-ui-plan.md) |
| M11b | 可信联网检索（搜索 backend + 安全抓取 + 来源） | ✅ · [方案](docs/archive/phase4/m11b-m11c-network-and-mcp-self-service-plan.md) |
| M11c | MCP/Skill 自助管理、授权与产物治理 | ✅ · [方案](docs/archive/phase4/m11b-m11c-network-and-mcp-self-service-plan.md) |

> **M11a 已完成**：ToolDisplay 让工具提供 UI 无关的动作/目标/摘要；normal 默认只显示语义轨迹，
> verbose 显示有界脱敏详情，quiet 只输出结果；流式 Markdown、`/display`、`run --quiet`、Run ID
> 收敛与紧凑分组 banner 落地；normal 使用临时活动区，工具间旁白不再堆积，Markdown 刷新限制为 15 FPS。
> CRUD 轨迹实测通过，411 passed、2 skipped、覆盖率 80%；未修改 Loop，
> 还清 D19。

> **M11b/M11c 已完成**：新增 DuckDuckGo/SearXNG 可替换搜索、结构化来源和受限网页抓取；
> Skill 采用 `.agents/skills` project scope 与用户安装目录；MCP 支持 user/project 原子配置、隔离探测、
> 最小子进程环境、会话级工具/server 信任及 `/mcp` 自助控制面。真实 Playwright MCP 与 Skill 生命周期
> 验收通过。467 passed、3 skipped、覆盖率 82%，18/18 scripted 与 4/4 recovery eval 全绿；未修改 Loop。

### 第五阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M12a | 通用 MCP 运行时安全语义 | ✅ · [方案](docs/archive/phase5/m12a-mcp-runtime-safety-plan.md) |

> **M12a 已完成**：稳定 call/trace/session/run ID 通过 MCP `_meta` 透传；受信 tool annotations
> 与 per-tool policy 控制只读恢复、超时和结果未知语义；structuredContent 按 outputSchema 校验。
> 配置管理允许非敏感环境字面量，同时继续阻止明文密钥。通用 stdio 握手和工具发现通过，
> 483 passed、3 skipped、覆盖率 82%，Ruff/mypy 全绿；未修改 Loop，不包含任何业务 MCP 实现。

### 第六阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M13a | 声明式工具适配层 | ✅ · [方案](docs/archive/phase6/m13a-declarative-tool-plan.md) |

> **M13a 已完成**：新增 `@agent_tool`、`FunctionTool` 和 `PermissionResolver`；类型注解生成
> Draft 2020-12 Schema，支持默认值、Literal/Optional/容器、上下文注入和未知字段容忍。
> 未声明权限时沿用未知扩展的保守声明，声明权限后仍由 Registry 强制决策。497 passed、3 skipped、
> 覆盖率 82%，Ruff/format/mypy 全绿；新增模块 162 行，未修改 Loop，未迁移现有工具。

### 第七阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M14a | 可靠中断与跨平台进程树监管 | ✅ · [方案](docs/archive/phase7/m14-controlled-execution-runtime-plan.md) |
| M14b | 统一 Workspace 执行边界 | ✅ · [方案](docs/archive/phase7/m14-controlled-execution-runtime-plan.md) |
| M14c | Docker/Podman 可选容器 Workspace | ✅ · [方案](docs/archive/phase7/m14-controlled-execution-runtime-plan.md) |

> **M14 已完成**：RunControl 支持第一次暂停、第二次强制取消；RunState v2 增加 cancelled；
> ProcessSupervisor 使用 Windows Job Object/POSIX process group 清理受管进程树并还清 D18。
> 文件/Shell/Git 统一经过 Workspace；容器默认仅挂载当前项目、无网络、非 root、清空 capabilities，
> 并设置 CPU/内存/PID 限制。526 passed、5 skipped、覆盖率 82%，Ruff/format/mypy 全绿。
> M14a 经用户授权修改 Loop，M14b/M14c 未修改 Loop。

### 第八阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M15 | CLI 活性反馈与关键动作可见性 | ✅ · [方案](docs/archive/phase8/m15-cli-activity-feedback-plan.md) |

> **M15 已完成**：统一 ActivityController 以单个 Live 展示模型、分析、工具和授权后执行阶段；
> 当前阶段耗时动态增长，正文停更 1 秒后显示模型仍在生成，连续等待超过 8 秒显示暂停提示。
> normal 仅落文件变更预览和外部动作意图，quiet 不增加过程输出。545 passed、5 skipped、覆盖率
> 83%，Ruff/format/mypy 全绿；未修改 Loop。

### 第九阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M16 | Agent 公共服务运行时边界 | ✅ · [方案](docs/archive/phase9/m16-service-runtime-boundary-plan.md) |

> **M16 已完成**：公共 Runtime 工厂不依赖 CLI/UI，固定 config/workspace 并逆序回滚资源；同步
> InteractionPort 覆盖授权、澄清、续跑、定义变化和 uncertain recovery；SessionRuntime 统一
> Session/Run/checkpoint/终态同步并保证单 Session 单 Run；StepEvent v1 标记 sensitive reasoning
> 和无歧义 run_terminal。566 passed、5 skipped、覆盖率 83%，Ruff/format/mypy 全绿；未修改 Loop。

### 第十阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M17 | CLI/Web 双入口与生产 Runtime 策略 | ✅ · [方案](docs/archive/phase10/m17-production-service-runtime-plan.md) |

> **M17 已完成**：RuntimePolicy 从调用方侧限制扩展管理、personal Skill、MCP transport 和最低
> sandbox；MCP optional/required、connect/call timeout 分离、有界并行启动和稳定注册顺序落地；
> RuntimeCapabilities 提供脱敏 Tool/Skill/MCP/sandbox 快照及一次性探测。577 passed、5 skipped、
> 覆盖率 84%，Ruff/format/mypy 全绿；未修改 Loop。

### 第十一阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M18 | 运行可解释性、预算恢复与结构化失败 | ✅ · [方案](docs/archive/phase11/m18-run-explainability-and-budget-continuation-plan.md) |

> **M18 已完成**：StepEvent v1 向后兼容增加 RunFailure/activity/BudgetSnapshot；RunState v3 保存
> failure、三类 continuation 上限/次数/决策并支持 v1/v2 迁移；Provider 429/5xx/timeout、工具、
> 权限、依赖和未知副作用形成稳定分类；`final` 与唯一 `run_terminal` 规则保持。594 passed、
> 5 skipped、覆盖率 84%，Ruff/format/mypy 全绿。经用户授权修改 Loop，并按架构硬线拆出
> continuation、resume 和定义兼容模块。

### 第十二阶段（已完成）

| 里程碑 | 主题 | 状态 |
|--------|------|------|
| M19 | Ports and Adapters 架构重建 | ✅ · [方案](docs/archive/phase12/m19-architecture-reconstruction-plan.md) |

> **M19 已完成**：公共 DTO 统一归属 contracts；Agent Context/Run、Provider/Tool ports、Application
> 用例、Bootstrap composition root 与 Service facade 分层落地；基础设施归入 execution、persistence、
> observability 与 integrations。旧导入保留 identity-compatible 薄转发。606 passed、5 skipped、
> 覆盖率 84%，12/12 import-linter、Ruff、mypy、scripted 18/18、recovery 4/4 全绿；经用户授权
> 修改 Loop，StepEvent v1、checkpoint v3、事件顺序、权限、预算和恢复语义保持兼容。

## 未来方向（P3，信号驱动，暂不做）

| 方向 | 触发信号 |
|------|---------|
| 拒绝确认时附原因回传模型 | M2.5 尾巴，想让模型据拒绝原因换做法时做 |
| D5：补 UI 层测试（console/main）| 触及相关改动时补 |
| D6：provider/model 分层重构 | 同厂商挂 4+ 模型、重复条目变烦时 |
| 覆盖 MCP/自定义 Tool 的完整隔离或远程 Workspace | 要跑不可信扩展 / 全自动无人值守 / 给别人用时 |
| 子 Agent 编排、Web GUI、rewind/recap | 需求明确且前置能力成熟时 |
| 非交互 init、PyPI/pipx 分发、macOS·原生 Linux 真机验证 | 分享/多平台推广时 |
| D12：选择性/检索式记忆（替代整段摘要）| 长会话里早期具体事实被摘要糊掉、后续又要精确引用时 |

---

## 设计哲学：Loop Engineering（贯穿所有里程碑）

> 参考 2026 年业界共识（Addy Osmani、Claude Code / Codex 作者）：agent 的质量不取决于
> 单次提示词，而取决于「观察→推理→行动→再观察」这个**循环**设计得好不好——
> "Build the Loop, Not the Prompt"。

这不是一个里程碑，而是评判每个里程碑的工程视角。我们的循环设计围绕六个关注点，
后续所有开发都应对照检查：

| 关注点 | 现状 | 归属 |
|--------|------|------|
| 循环终止（防跑飞/死循环）| ✅ `max_iterations` | 已有 |
| 上下文管理（不爆窗、留对的东西）| ✅ token 感知截断 + 预算口径含 schema/reserved + 摘要压缩 | M3/M8a/M8b |
| 错误恢复（工具失败循环不崩）| ✅ ToolResult 归一 + 步骤级 checkpoint/恢复 | M10a/M10b |
| 反馈质量（喂回模型的观察够好）| ✅ 基本具备 | 已有 |
| 状态可见性（能看到循环在干嘛）| ✅ 流式 + 分阶段状态 + JSONL 事件日志 | M2/M6 |
| 成本 / token 可见与控制 | ✅ token 用量显示 + 运行时预算 + 上下文占用% | M2/M6.5/M8a |

> `agent/loop.py` 是稳定内核、`tools/` 与 `llm/` 是隔离扩展点——这套结构本身就是
> Loop Engineering 的落地。加能力优先走扩展点，不动内核（铁律 4）。

---

## M2 — 流式与过程透明（P0）✅ 已完成

**交付**：LLMClient 流式（碎片拼接实测 DeepSeek 后实现）、AgentLoop 消费流式（动内核，控制流不变）、
Console 流式渲染（Live spinner 与正文时间错开）、show_reasoning 开关、token 用量 + 任务计时显示。
35 测试全绿，云端 DeepSeek 实测流式通过。收尾时从 Claude Code 截图学习补充了计时（`耗时 Xs`）
与 token 方向（`↑输入 ↓输出`）显示。

**解决**：发问后长时间"黑屏"，思考/等待过程不透明。

**范围**：
- 流式 token 输出（`litellm` `stream=True`）
- 思考（reasoning）内容实时显示（DeepSeek 的 `delta.reasoning_content`）
- spinner + 分阶段状态：等待网络 → 思考 → 生成回复 → 调用工具
- 本地模型无 reasoning 时优雅降级为普通 spinner
- **token 用量显示**（statusline）：每轮/累计 token 消耗可见，补上"成本可见性"（Loop Engineering 条目，参 Codex/Claude Code 的 statusline）

**关键技术点**：流式下工具调用参数**跨 chunk 碎片化到达**，需按 index 累积拼接（最大坑点）。token 用量从流式响应的 usage 字段获取（部分后端需开启 `stream_options={"include_usage": True}`）。

**动内核**：`LLMClient.complete` 增加流式路径；`AgentLoop.run` yield 更细粒度的增量事件；`Console` 改流式渲染。破铁律第 4 条一次，需保证现有测试仍绿。

**已定决策**：reasoning 思考内容通过配置开关 `show_reasoning` 控制，**默认折叠**（只显示"思考中…" spinner），打开时灰字实时滚动。本地模型无 reasoning 时天然只显示 spinner，无需额外降级逻辑。

**验收标准**：
1. 同一任务发问后 **3 秒内有可见反应**（不再黑屏）
2. 思考过程实时滚动显示（有 reasoning 的模型）
3. 流式下工具调用**正确执行**（碎片拼接无误）
4. 云端 DeepSeek 与本地 LM Studio 都不卡黑屏
5. **token 用量可见**：任务结束后能看到本次消耗的 token 数
6. 现有 28 测试全绿 + 新增流式相关测试

---

## M2.5 — 任务中确认机制升级 + 中断（P1）

**解决**：当前只有 shell 危险命令的 y/n 确认，且仅限 shell。学习 Claude Code 的"人在环中"
授权机制——任务执行到需要授权时弹出多选，用户选择后继续。

### 两层用户选择设计（核心，已实现前半）
把 agent 需要用户介入的场景明确分成两类，语义不混：

| | 层1：模型层澄清 | 层2：工具层权限确认 |
|---|---|---|
| 语义 | 确认**意图**（要什么/怎么做） | 确认**授权**（准不准执行这个危险操作） |
| 触发 | 模型自己判断（提示词驱动） | 工具声明需确认，框架强制拦截 |
| 场景 | 需求歧义、方案选择、业务判断 | 删除/覆盖/移动/执行命令/联网/装依赖 |
| 形式 | 自然语言提问，可列 1/2/3 | 运行时审批（允许/永久允许/拒绝） |
| 代码 | 提示词 `build_system_prompt(interactive)` | `ToolContext.request_confirm(category, msg)` |

- **顺序**：一个操作若既歧义又危险 → 先层1澄清（问清方案），方案定了再调工具（层2拦截确认）。
- **模式差异**：层1澄清在 `chat`（多轮）可提问等待；在 `run`（单次）无法等回答，
  改为**按最合理假设执行并说明假设**（`build_system_prompt(interactive=False)`）。

### ✅ 已实现
- `ToolContext.request_confirm`（允许/永久允许/拒绝三选）+ 按类别记忆"永久允许"。
- ShellTool 改用之；`Console.confirm` 多选交互（提示前停 spinner、补换行防输入污染）。
- 提示词编码两层：层1澄清（chat问/run假设）、层2权限（不自然语言问危险、直接调工具）。
- run/chat 模式经 `_setup→AgentLoop→Conversation→build_system_prompt` 传递。

### ⏸ 待做（M2.5 后半）
- 拒绝时附带说明反馈给模型（当前只是拒绝，未回传原因）。
- 确认机制在更多工具上生效（等 M4 有网络/装依赖等工具时接入）。

**验收标准**：
1. ✅ 危险 shell 命令弹出多选（允许/永久允许/拒绝），选择后正确继续或中止
2. ✅ "永久允许"后同类操作本会话不再询问
3. ✅ 两层语义分开：run 模式遇歧义自行假设、不冗余提问；危险操作走工具层确认
4. ⏸ 拒绝并附说明时，模型能据此换做法
5. ✅ 长任务执行中可用 Ctrl+C 中断，保留已输出、干净收尾（选 Ctrl+C 而非 esc：跨平台可靠）
6. ✅ 现有测试全绿 + 新增确认/两层/模式/中断测试（42 通过）

---

## M3 — 记忆与会话持久化（P1）— 已完成 ✅

**解决**：跨会话零记忆，下次重启忘光。

**已实现**：
- **上下文工程（短期）**：`context.py` 截断从"消息数"升级为 **token 感知**（字符估算，CJK 安全偏保守；保留 system+最近消息应对 lost-in-the-middle）；`max_context_tokens` 配置（默认 8000）。
- **Agent Memory（中期）**：`session/store.py`（JSON，项目 `./.assistant_agent/sessions/`）save/load/list/delete；`Conversation.export_history/load_history` 序列化（不含 system）；CLI：`chat` 默认新会话+每轮自动保存、`chat --resume <id>` 续接、`sessions` 列出、`sessions --delete <id>`（带确认）。
- **长期（向量检索）**：❌ 明确不做。

**验收标准**：
1. ✅ `chat` 退出重启后，`sessions` 能列出历史会话
2. ✅ `--resume <id>` 能恢复并续接（export/load history 实测往返一致）
3. ✅ `sessions --delete <id>` 能删除（带确认）
4. ✅ 上下文截断改为 token 感知（含消息数硬上限兜底、不破坏 tool 配对）
5. ✅ 新增测试全绿（68 通过：test_session/test_context/test_client 等），ruff + 架构测试通过

**顺带还债**：D1（流式碎片拼接补直接单测）、D4（Console.input 收口）已还，见 docs/TECH_DEBT.md。

---

## M4 — 工具集扩展（P1–P2）— 已完成 ✅

**解决**：工具集太窄，做不了真实开发任务。

**已实现**（纯走 `tools/` 扩展点，内核未动；方案见 docs/archive/phase1/m4-tools-plan.md）：
- **code_search**（`tools/search.py`）：纯 Python grep，跨平台（Windows 可用），只读不确认；支持 pattern/path/glob/ignore_case/max_results。
- **git 只读**（`tools/git.py`）：单工具 + 子命令白名单（status/diff/log/show/branch）；写操作拒绝；shell=False 防注入；args 经 shlex 解析。

**明确未做**：web_fetch/网络搜索（安全边界不成熟，暂缓）、glob/find_files（可选，暂缓）、git 写操作（不可逆，不做）。

**验收标准**：
1. ✅ 每个新工具各带单元测试（test_search 9 + test_git 7）
2. ✅ 能完成真实链路："搜索代码 → 读取 → 修改 → git diff 确认"（实测通过）
3. ✅ 危险操作纳入机制：git 写子命令被白名单拒绝；只读工具不确认（对齐 Claude Code）
4. ✅ 现有测试仍绿（84 通过），ruff + 架构测试通过

---

## M4.5 — 模型管理与切换（P1）— 已完成 ✅

**解决**：模型切换只能"改 config.yaml + 重启"，不灵活。

**已实现**（方案见 docs/archive/phase1/m4_5-model-management-plan.md）：
- **`--provider/-p` 启动标志**：run/chat 临时指定后端，覆盖 config.active，不改文件；非法名报错列可选。
- **对话内 `/model`**：chat 输入 `/model` 弹方向键菜单（复用 questionary）选择、`/model <名>` 直切；**切换保留对话历史**（M3 的 token 截断兜底更小窗口）。
- **`providers` 命令**：列出所有 provider（名/模型/云端或本地/当前标记）。
- **内核轻碰（已批准）**：`AgentLoop.set_client` 仅换 client、不改 run() 控制流，历史天然保留。

**验收标准**：
1. ✅ `--provider <名>` 覆盖生效；非法名报错列可选（实测）
2. ✅ `/model` 菜单/直切，切换保留历史（set_client 单测证明 export_history 前缀不变）
3. ✅ `providers` 列出所有 provider（实测表格含云端/本地/当前）
4. ✅ 切换无副作用、不触发确认
5. ✅ 90 测试全绿，ruff + 架构测试通过；内核仅加 setter，控制流未改

---

## M4.7 — 循环工程与写入安全（P1）— 已完成 ✅

**解决**：真实使用发现 ① agent 复杂循环里自作主张改文件（write_file 无范围限制）；② 用尽轮数硬失败、卡死空耗。

**已实现**（方案见 docs/archive/phase1/m4_7-loop-engineering-plan.md）：
- **工作区范围写入**：写项目目录内直接放行，写目录外需确认（对齐 Codex workspace-write / Claude acceptEdits）；靠"流式可见 + Ctrl+C + git"兜底，不逐个弹窗。
- **重复动作熔断**（内核）：连续 3 轮完全相同的工具调用 → 判定卡死终止，不空耗到上限。
- **用尽轮数优雅**（内核）：注入 continue_check——chat 问"继续吗"、run 带如何继续的提示停。
- **`--max-iterations`**：覆盖 config。
- 提示词：只改任务相关文件、改前说明范围。
- 内核改动：run() 加"重复检测 + 用尽续跑"分支 + continue_check 参数，控制流主体未变。

**未做（按方案）**：权限模式（readonly/strict）——可选，后置；OS 沙箱——过重，记为未来信号（不可信/全自动时才需）。

**验收标准**：
1. ✅ 区内写放行、区外写确认、拒绝不写（实测）
2. ✅ 连续相同动作达阈值熔断（单测 client.calls==3）
3. ✅ chat 用尽问续、run 优雅提示停（单测）
4. ✅ --max-iterations 生效（实测）
5. ✅ 96 测试全绿，ruff + 架构测试通过（护栏逼出 console.py 拆分至 formatting.py）

---

## M4.8 — 基础工具补全：局部编辑（P1）— 已完成 ✅

**解决**：只有 write_file 整篇重写——改一行也要模型重输出整个文件，费 token、慢、易误伤其余内容。

**已实现**（方案见 docs/archive/phase1/base-tools-plan.md）：
- **edit_file**：精确替换（old_string→new_string），唯一匹配才改（防误替），支持 replace_all；对齐 Claude Edit。
- **multi_edit**：同文件多处替换，顺序应用、原子写入（任一失败整体不改）；对齐 Claude MultiEdit。
- 沿用工作区范围（区外编辑需确认）；提示词引导"局部改优先 edit_file"。
- 调研结论：各家"局部改"本质是 SEARCH/REPLACE（Claude Edit、Roo/Cline apply_diff 底层），
  **不做行号 unified diff**——对本地小模型太脆弱，违背"对笨模型健壮"原则。

**验收标准**：
1. ✅ edit_file 唯一替换；未找到/多次歧义/文件不存在 → 清晰 error（实测）
2. ✅ replace_all 替换所有；multi_edit 原子中止（单测覆盖）
3. ✅ 区外编辑走确认
4. ✅ 110 测试全绿（+8 edit 测试），ruff + 架构测试通过；内核未动

---

## M4.9 — Slash 命令系统（P1）— 已完成 ✅

**解决**：`/model` 等能力用户不知道存在（无可发现性）；控制命令散在 chat 循环的 if 里。

**已实现**（方案见 docs/archive/phase1/slash-commands-plan.md）：
- 新增 `cli/commands.py`：SlashCommand/SlashRegistry/ChatContext（仿 ToolRegistry）。
- 内置命令：`/help`（列出全部+说明，可发现性核心）、`/model`、`/sessions`、`/clear`（新会话）、
  `/context`（会话状态/用量）、`/exit`。收编原散落的 /model 与 exit。
- 本地拦截、不进 ReAct、不花 token（对齐 Claude）；未知命令友好提示。
- 基础档（纯打印、鲁棒）；实时下拉菜单（prompt_toolkit）作为后续可选增强，本期不做。

**验收标准**：
1. ✅ `/` 或 `/help` 列出所有命令+说明（实测）
2. ✅ /model /sessions /clear /context /exit 各生效（实测 + 单测）
3. ✅ 未知 /xxx 友好提示、不进 ReAct
4. ✅ slash 本地处理、不调模型（0 token）
5. ✅ 119 测试全绿（+9 命令测试），ruff + 架构测试通过（新增 cli 层）；内核未动

---

## M5 — 上手体验（P2）— 已完成 ✅

**解决**：新用户/新机器上手门槛（安装 + 手动 cp config、填 key）。

**已实现**（方案见 docs/archive/phase1/m5-init-plan.md、docs/INSTALL.md）：
- **安装与平台支持**：`docs/INSTALL.md` 各平台步骤 + 矩阵（Windows 原生/Git Bash/WSL2 已实测；Linux/macOS 高置信；Termux 非目标）。
- **`assistant-agent init` 交互向导**（`cli/init.py`）：选后端（云端 OpenAI 兼容/Anthropic/本地）→ 配 model/env/端点 → 检测 → 生成 config.yaml → 校验。
- **安全**：复用 `${VAR}` 展开（不改 schema）；config 只写 `${环境变量名}`，**init 默认不读/不写真实 key**，只检测变量是否已设并给设置指引；本地端点写占位 key；已存在 config 先备份不静默覆盖；端点检测带 timeout + 本地关代理。
- **非交互模式**：暂缓（接口预留）；init 在无 tty 时明确拒绝。

**验收标准**：
1. ✅ init 一路问答生成可用 config.yaml（云端 `${VAR}`／本地占位+api_base；已存在则备份）
2. ✅ 本地端点连通检测（成功报模型数/失败/超时三态，mock 测试覆盖）
3. ✅ 生成配置经 loader 校验通过；生成物不含明文 key（脱敏回归测试）
4. ✅ 135 测试全绿（+16 init 测试），ruff + 架构测试通过；内核未动

---

## M6 — 结构化日志与工具审计（第二阶段第一项）— 已完成 ✅

**解决**：agent 每一步（调什么工具、参数、耗时、成败、危险操作的授权决策）跑完不留痕——不可观测、不可审计，也挡住后续多 Agent/沙箱/生态接入的调试。还清第一阶段"无结构化日志"债。

**已实现**（方案见 [归档文档](docs/archive/phase2/m6-observability-plan.md)，**内核未动**）：
- **新增 `obs/` 层（rank 0）**：`EventLogger`（JSONL 按天分卷）+ `NullLogger`（禁用时零副作用）+ `create_logger` 工厂 + 脱敏截断。登记进架构测试 `_LAYER_RANK`，护栏强制"obs 不依赖上层"。
- **两个落点（皆在内核外）**：`ToolRegistry.execute` 计时 + `tool_call` 事件；`ToolContext.request_confirm` 记 `confirm` 审计（allow/always/deny + 是否命中永久允许记忆）。
- **会话生命周期**：`main._setup` 构建注入 logger 并记 `session_start`；run/chat 记 `task` 与 `session_end`。
- **配置 `LoggingConfig`**：enabled / dir / log_tool_io / max_payload_chars；`config.example.yaml` 补示例。日志落 `.assistant_agent/logs/`（随 gitignore 不入库）。
- **隐私**：参数/输出截断 + 尽力脱敏（sk-/ghp_/AKIA 等前缀 + 敏感键名遮蔽；刻意不做"任意 32+ 长串"以免误伤正文）；写入非致命（异常吞掉，绝不因日志中断任务）。

**验收标准**：
1. ✅ 跑一次带工具调用的任务后生成 `logs/<日期>.jsonl`，含 session_start + tool_call（带耗时/状态），每行可 `json.loads`（冒烟实测 + 单测端到端）
2. ✅ 危险操作授权留痕（confirm 事件 allow/always/deny）
3. ✅ 日志不出现明文密钥（脱敏回归测试；冒烟实测 sk- 被遮蔽）
4. ✅ `enabled=false` 走 NullLogger、零副作用
5. ✅ 写入失败非致命
6. ✅ **内核 `agent/loop.py` 未动**；155 测试全绿（+20：test_obs 15 + config 2 + 架构 obs 登记等），ruff 全绿
7. ✅ 新增测试覆盖 obs logger、两个落点、配置解析

**顺带**：D7（main.py 逼近 300 行）——本期曾触线 305，以"logger 构建外移到 obs.create_logger"化解回 298。复盘另发现 4 项观测缺口登记为 D8（duration 含确认等待、脱敏不递归、/clear 与 /model 后日志元信息不更新），均非阻断、留信号驱动。债册已更新。

**未做（留后续）**：`/audit` 命令、把 final/error/interrupted 循环结果落日志、日志自动清理、记录完整 LLM prompt/response。

---

## M6.5 — 运行时预算与工具协议完整性 — 已完成 ✅

**解决**：`max_iterations` 只限制模型轮数，无法限制单轮批量工具调用；工具结果也只有单次截断，缺少任务累计边界。预算在批次中途耗尽时，还必须保证每个 assistant tool call 都有对应 result。

**已实现**（方案见 [归档文档](docs/archive/phase2/m6_5-runtime-budget-plan.md)）：
- `tools.max_output_chars`：单个工具结果上限，旧 `agent.max_tool_output_chars` 自动迁移。
- `agent.max_tool_calls`：单任务工具调用总数；`agent.max_total_tool_output_chars`：累计工具结果字符预算。
- `ToolBudget` 状态放在 `ToolContext`，Registry 保持无任务状态；每次 `AgentLoop.run()` 安装独立预算并在结束时恢复。
- 单轮多工具调用超预算时，未执行调用仍得到明确错误 result；整批补齐后统一终止，不留下悬空 tool call。
- 审计补齐完整墙钟耗时、确认等待、近似执行耗时、原始/返回输出长度和 `budget_exhausted` 事件。
- 修复 Windows 测试使用 Unix `rm` 的跨平台基线问题。

**内核改动**：经用户确认，`agent/loop.py` 只增加任务预算生命周期和批次终止逻辑；流式接口、UI/provider 依赖、重复熔断和 continue_check 语义不变。

**验收**：179 测试全绿，Ruff 全绿；关键路径覆盖配置迁移、调用/输出预算、跨轮累计、单轮多调用协议完整性、预算重置、预算审计和迭代续跑不重置预算。本地 LM Studio `/v1/models` 一度可用，但生成请求时服务退出并拒绝连接，因此真实模型三类冒烟未完成，已按外部环境阻塞如实记录。

---

## M7a — Agent Skills 系统（第二阶段·生态接入其一）— 已完成 ✅

**解决**：能力全靠内置工具 + 静态提示词，无法复用"针对某类任务的做法手册"。Skill = 可复用指示书（SKILL.md 文件夹），模型按需加载、按其指示用现有工具完成任务。

**已实现**（方案见 [计划文档](docs/archive/phase2/m7a-skills-plan.md)，**内核仅轻碰**）：
- **新增 `skills/` 层（rank 2，叶子能力）**：`SkillStore` 扫描 `./.assistant_agent/skills/` 与 `~/.assistant_agent/skills/`，解析 SKILL.md frontmatter；坏文件跳过不崩、同名"项目覆盖个人"。
- **渐进披露三级**：L1 启动只注入 name/description（几十 token/个）；L2 模型调 `load_skill(name)` 返回正文；L3 正文指向的脚本/参考文件由模型用现有 read_file/run_shell 读或跑（零新机制）。
- **prompt 动态注入**：`build_system_prompt(interactive, skills)` 加"# 可用技能"节；复用 `Conversation` 已有的 `system_prompt` 接缝——`/clear`/`/model` 都不动 system，注入一次天然存活。
- **安全**：技能脚本经现有 shell 工具 → 自动走危险确认门；`load_skill` 只按已发现名查、不接受路径（杜绝穿越）；文档提示第三方技能需代码审查。
- **配置 `SkillsConfig`**（enabled/dirs）；`/skills` slash 命令列出已发现技能。

**内核改动**：`agent/loop.py` 仅给 `AgentLoop.__init__` 加 `system_prompt` 透传参数（转发给 Conversation 已有的同名参数），run() 控制流零改动，风险等同 M4.5 的 set_client。

**验收**：194 测试全绿（+15：解析/发现/去重/加载/工具/注入/路径穿越/回归），ruff + 架构测试通过（skills 登记 rank 2）。端到端冒烟实测：真实 SKILL.md 发现 → 注入提示词 → load_skill 返回正文，三级跑通。

**顺带**：D7 升级——技能接线把 main.py 推到 329 行、越软线 300（非阻断警告）；评审判断 wiring 组经 `_interrupt` 全局耦合、收尾期不硬拆，登记为 D7 专项（M7b 再加接线时抽 `cli/setup.py`）。

**未做（留 M7b/M7c）**：MCP client、远程技能拉取、技能热重载。

## 延后（P3，不进近期路线）

- **子 Agent / 多 Agent 编排**：复杂，需求未明确，依赖前面能力成熟。
- **完整沙箱隔离**：仅在"跑不可信任务"时刚需，自用可暂缓（当前靠危险命令确认兜底）。
- **Web GUI**：已决定放到很后面；需先给 Agent 加 HTTP/流式 API 层。

---

## 通用工作纪律（每个里程碑都遵守）

- 改完跑 `pytest` + `ruff`，全绿再算完成（铁律 3、5）。
- 能不动内核就不动；必须动时（如 M2）保证现有测试不回退。
- 每完成一个里程碑做一次 git 提交，留可回退锚点。
- 里程碑退出前同步状态文档：本文件的里程碑表 + 顶部"项目当前状态"块，及 CLAUDE/AGENTS/README 的"当前状态"段；数字用实测、不凭记忆（详见 DoD 第 6 条）。
