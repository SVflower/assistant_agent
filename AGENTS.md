# AGENTS.md

> 始终使用中文与我交互。
> 给 Codex 的项目说明。保持精简——这个文件每轮都进上下文。
> 详细设计见 [DESIGN.md](DESIGN.md)。

## 项目是什么

一个跑在本地、**模型后端可自由切换**（云端 API key / 本地 LM Studio / vLLM）的通用任务 Agent，编码能力优先。
核心卖点：换模型只改 `config.yaml`，业务代码零改动。

## 技术栈

- Python 3.11+
- LiteLLM（模型统一层，所有 provider 走 OpenAI 兼容格式）
- Pydantic + YAML（配置）
- Typer（CLI）
- pytest（测试）

## 命令

```bash
# 安装依赖（开发模式）
pip install -e ".[dev]"

# 跑测试
pytest

# 跑单个测试文件
pytest tests/test_tools.py

# 覆盖率（让未测盲区显形）
pytest --cov

# 格式化 + lint
ruff format . && ruff check --fix .

# 启动 agent
python -m assistant_agent
```

## 铁律（必须遵守）

1. **绝不在业务逻辑里写死 provider。** 所有模型调用走 `llm/client.py` 的抽象层。换后端是改配置的事，不是改代码的事。
2. **绝不提交密钥。** API key 只进 `config.yaml`（已 gitignore）或环境变量。`config.example.yaml` 永远不含真实 key。
3. **改完代码必须跑 `pytest` 和 `ruff`**，确认通过再说完成。
4. **内核职责稳定、实现受控演进**：`agent/loop.py` 是内核。加能力**优先** = 在 `tools/` 加文件并注册；确需改循环（预算/终止/恢复等内核职责）时允许，但**改动前必先向用户确认、改后现有测试不回退**。
5. **新功能要带测试。** 工具、配置、循环的改动都要有对应测试。

## 约定

- 工具实现放 `tools/`，继承 `base.py` 的基类，在 `registry.py` 注册。
- 自研 MCP 统一放在 `D:\Dev\mcp\<server_name>`，独立管理源码、测试和依赖；Agent 仓库只保留
  MCP client、配置和接入文档，不内嵌业务 MCP 源码或测试。
- shell 工具：删除/覆盖/移动等危险操作前必须向用户确认；普通命令直接执行。
- 上下文管理要做长度感知截断——本地模型上下文窗口比云端小得多。
- 错误处理要对"笨模型"健壮：本地小模型的工具调用格式经常不规范，解析要容错、要重试。

## 里程碑工作流（较大任务默认遵守）

做较大任务（新里程碑、新特性、动多文件）时，默认走这套流程，不用用户每次提醒；
小改动（改 bug、单文件微调）可直接做。

1. **调研**：参考成熟产品/公开资料，总结可借鉴原则（不照搬），拿不准的标注"不确定"，不编造。
2. **评估**：读现有代码，判断当前架构是否适合扩展、是否需要动内核。
3. **方案**：落成 `docs/<里程碑>-plan.md`，含范围（必做/可选/不做）、技术设计、是否动内核、测试计划、验收标准、风险边界。
4. **审阅**：先出方案，用户确认后再写代码；**动内核 `agent/loop.py` 前必须先问用户**。
5. **实现**：分步进行，每步带测试。
6. **验收**：按方案的验收标准 + 下面的 DoD 全绿，才算完成。

## 质量护栏（防迭代劣化）

- **架构适应度测试** `tests/test_architecture.py`：自动检查分层依赖（config→llm→tools→agent→ui→main，只能依赖同层或更低层）、内核 UI 无关、工具不反向依赖、单文件行数（软线 300 仅警告交人评审、硬线 500 才失败）。**报红时应拆分/修依赖，而不是放宽规则。**
- **技术债登记册** `docs/TECH_DEBT.md`：新债即时登记，每次里程碑评审更新，防隐形复利。
- **覆盖率** `pytest --cov`：不设强制门槛，但关键路径（流式碎片拼接、confirm 解析）低覆盖要显形并补测。
- **跨项目契约同步**：`docs/agent-service-integration-guide.md` 是 Agent 对 API 及其他调用方的长期正式契约。凡里程碑修改 `assistant_agent.service` / `assistant_agent.interaction` 公共出口、StepEvent/DTO、Interaction、Run/Session 状态、失败码、生命周期或兼容语义，必须在同一里程碑同步该文档、契约版本/迁移说明、完整事件序列和契约测试；归档 plan/handoff 只能记录历史，不能替代正式契约。破坏性变化必须提升契约版本；向后兼容扩展也必须写明。若确认无影响，方案和验收报告必须明确记录“公共服务契约无变化”及依据。阶段收尾时还必须输出一份可直接交给 API 项目 AI 的变更清单，包含 Agent commit、API 必改项、兼容影响和联调测试。

## 里程碑完成定义（DoD）

每个里程碑退出前必须全部满足：
1. `pytest` 全绿（含架构测试），`ruff check` 全绿。
2. 本里程碑新增/改动的**关键路径有测试**（不追全覆盖，但别留脆弱逻辑裸奔）。
3. 发现的新技术债已登记进 `docs/TECH_DEBT.md`。
4. 无密钥/垃圾文件入库（提交前审查 `git diff --cached`）。
5. 动了内核 `agent/loop.py` 时，说明理由并确认现有测试不回退。
6. **状态文档同步**：更新 ROADMAP 里程碑表的状态标记 + 顶部"项目当前状态"块，以及本文件、`CLAUDE.md`、`README.md` 的"当前状态"段。数字（测试数/覆盖率/源码行数/剩余技术债）用**实测**（`pytest -q`、`--cov`、`wc -l`），不凭记忆。里程碑历史小节里的旧数字是当时快照，不回改。
7. **服务契约闭环**：完成跨项目契约影响检查；有影响时正式契约、版本/迁移说明、契约测试和 API AI 变更清单全部同步，无影响时留下明确结论。缺任一项不得把里程碑标记完成。

## 当前状态

**第一至第十一阶段已完成**。里程碑详情见 ROADMAP.md。
- 第一阶段：配置/模型抽象/工具/ReAct 循环/CLI，加流式输出、会话持久化、工具集扩展（edit/multi_edit/code_search/git 只读）、模型切换、循环工程与写入安全、slash 命令、init 向导，全部落地。
- **第二阶段 M6/M6.5/M7a/M7b/M7c/M8a/M8b 已完成**：结构化日志与工具审计；任务级工具调用/累计输出预算与批次协议完整终止；Agent Skills 系统（SKILL.md 发现 + 渐进披露 + load_skill）；MCP client（stdio + HTTP 两种 transport）——外部 server 工具接入 + 同步桥 + 命名空间 + 每工具确认 + 过滤/上限 + HTTP 委托 SDK 管 session/重连不重放 + cli/setup.py Runtime，还清 D7；上下文进化——M8a 预算口径计入 tools schema + reserved（还 D10），M8b 摘要压缩替代硬截断（双历史 + checkpoint 持久化 + 按轮分组 + 降级兜底，默认关闭时逐字节等于现状）。
- 双后端实测通过：云端 DeepSeek + 本地 LM Studio，切换只改 `config.yaml`，业务代码零改动。
- M9a 已完成：上下文最终硬封套、Session 路径限制与原子保存、Runtime/MCP 失败回滚、模型切换一致性、CI 与 mypy 基线。
- M9b 已完成：Registry 统一权限门、四种权限模式、精确授权、Shell 保守能力分析、Skill/MCP 信任边界与 observer。
- M9c 已完成：scripted/real 双轨行为 eval、14 个确定性案例、可解释 scorer、JSONL/Markdown 报告、A/B compare 与 CI 质量门。
- M10a 已完成：统一工具参数校验与结构化结果；大文件分页/流式搜索；Shell/Git 有界捕获与受限 Artifact；文件原子写；MCP structuredContent 保真。
- M10b 已完成：严格 RunState、双槽原子 RunStore、模型/审批/工具边界 checkpoint；已完成工具不重放，started 副作用需 retry/skip/abort；预算/熔断/权限/摘要跨进程恢复；`runs`/`resume`、Session 幂等同步和 trace/session/run/call 日志标识落地，还清 D8。
- M10c 决策完成：不进行全栈 async 重构；M14 已用同步运行控制和跨平台进程监管补齐缺口。
- M11a 已完成：ToolDisplay 语义摘要、normal/verbose/quiet、统一终端脱敏、15 FPS 流式 Markdown、`/display` 与 `run --quiet`；normal 过程文本仅在活动区显示，最终回答才落屏；Write/Edit 提供权限前有界代码预览/结构化 diff，并用整块代码底色和增删行背景区分；输入区全宽分隔并显示会话启停 ID；Console renderer 按职责拆分，还清 D19，未修改内核 Loop。
- M11b/M11c 已完成：结构化 Web 搜索与安全抓取；Skill user/project 安装管理；MCP 原子配置、隔离探测、会话级 server/tool 信任、最小子进程环境和 workspace 产物治理；Playwright MCP 与 Skill 的真实安装/使用/卸载闭环通过，未修改内核 Loop。
- M12a 已完成：通用 MCP 关联 ID 透传、受信 annotations、per-tool 恢复/超时策略、写调用 unknown
  保护、输出契约校验和非敏感环境字面量配置；任意业务 MCP 均外置接入，未修改内核 Loop。
- M13a 已完成：`@agent_tool` + `FunctionTool` 声明式适配层、Pydantic Schema、可选上下文注入和
  权限解析器；完整复用 Registry 安全链路，未修改内核 Loop。
- M14 已完成：第一次 Ctrl+C 可恢复暂停、第二次强制取消；Windows Job Object/POSIX process group
  清理受管进程树；Host/Confined/Container Workspace 统一文件与进程边界，容器默认无网络、非 root、
  仅挂载当前 workspace；Web/外置 MCP/自定义 Python Tool 仍明确位于宿主边界。M14a 经授权修改 Loop，
  M14b/M14c 未修改 Loop；还清 D18。
- M15 已完成：单 Live 动态阶段反馈、正文停更 1 秒后的模型生成提示、当前阶段等待计时、8 秒
  暂停提示、授权/继续后恢复动画，以及 normal 模式下文件变更预览和外部副作用意图；不展示
  隐藏推理，未修改 Loop。
- M16 已完成：UI 无关 Runtime 工厂、结构化同步 InteractionPort、隔离 SessionRuntime、
  Session/Run 公共门面和 StepEvent v1；CLI/API 复用同一装配与恢复语义，未修改 Loop。
- M17 已完成：部署级 RuntimePolicy、MCP optional/required 与连接超时分离、有界并行启动、
  脱敏 RuntimeCapabilities 和一次性能力探测；CLI 保持兼容，未修改 Loop。
- M18 已完成：结构化 RunFailure/activity/BudgetSnapshot；三类预算 continuation 统一经 InteractionPort
  并在继续前 checkpoint；Provider/工具/权限/依赖稳定分类；RunState v3 兼容迁移。经授权修改 Loop。
- 594 个测试通过（5 个平台能力测试跳过），覆盖率 84%，ruff/mypy 全绿；14930 行生产 Python
  源码 + 1564 行 eval 基础设施。

第三阶段总规划及 M9a-M10c 方案/决策已归档到 `docs/archive/phase3/`，还清
D8/D9/D13/D14/D15/D16/D17。剩余工作按技术债和真实触发信号立项，
不自动进入全栈 async 重构。M11a-M18 方案已分别归档到 `docs/archive/phase4/`、
`docs/archive/phase5/`、`docs/archive/phase6/`、`docs/archive/phase7/`、`docs/archive/phase8/`、
`docs/archive/phase9/`、`docs/archive/phase10/`、`docs/archive/phase11/`。
