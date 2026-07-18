# M19 项目架构重建方案

> 状态：已完成并归档。用户已授权 M19d 修改 `agent/loop.py`。
> 版本：v3（2026-07-19，吸收外部评审并完成实施可行性复审，替代 v2）。核心决策：
> ①砍掉低价值 churn——builtin 工具聚类、`ui/` 合并、测试四象限重排全部出局；
> ②`service/` 收敛为纯 re-export，允许转发 bootstrap composition root，但自身零逻辑；
> ③补齐 Run/Application/Tool 消费方端口，`tools/base.py` 不再只改名；
> ④恢复七阶段小步迁移，机械移动、状态机调整和文档收尾分开；
> ⑤保留未来能力落位图，但外部项目只依赖 service/contracts，不穿透 application。
> 分支：`codex/m19-architecture-reconstruction`
> 基线：M18，594 passed / 5 skipped，StepEvent contract v1，Run checkpoint v3。
> 前置条件已核实：M18 代码提交 `0ef635a`、正式契约提交 `fa97f52` 均已进入 `main`，
> 且为当前分支 HEAD 的祖先；M19c 不会与未完成的 M18 并行修改预算/恢复模块。

## 1. 目标与产品方向

产品定位：**企业级 AI 问答 Agent**。可预期的演进包括 RAG 检索、知识库连接器、
多通道接入（HTTP API / Web / IM 机器人）、审计与多租户。本次重构**不实现**这些能力，
但必须让它们未来进场时"有唯一明确的家"，而不是继续往顶层堆包。

架构目标（按重要性排序）：

1. **契约单一所有权**：公共 DTO/事件/错误只在 `contracts/`，API、CLI 和未来任何通道
   都朝它依赖，不再穿透内部实现目录取类型；
2. **概念单一所有者**：改 Run 语义只进 `agent/run/`，不再跳 9 个文件；
3. **核心零基础设施/厂商耦合**：Agent 核心不 import LiteLLM/Rich/Typer/MCP SDK/具体 Store，
   由 import-linter 机器强制，不靠自觉；
4. **编排唯一**：CLI 与 Python Service 复用同一 application 用例，未来新通道只是新适配器；
5. **扩展点显式**：§9 的每项未来能力都有预先声明的落位；本期不预建任何抽象；
6. **行为冻结**：现有行为、checkpoint v3、StepEvent v1 契约完全兼容，结构改造不夹带功能。

**明确放弃的目标：目录形态的完美主义。** 凡不服务于以上 1-5 的搬迁，一律不做（见 §14）。

## 2. 现状判断

当前代码有完整测试和成熟的运行语义，问题不是"不可用"，而是拓扑落后于产品：

- 顶层同时存在 `agent/runtime/service/session/interaction`，Run 和 Runtime 所有权不直观；
- `runtime/` 表示进程/Workspace/容器，`service/runtime.py` 又表示 Agent Runtime，术语冲突；
- 一次 Run 的行为散落在 `loop.py`、`execution.py`、`continuation.py`、`loop_resume.py`、
  `recovery.py`、`run_state.py`、`run_control.py`、`recovery_codec.py` 和 `service/sessions.py`；
- `agent/events.py`、`agent/failures.py` 是公共契约，却归属内部 Agent 实现；
- `service/sessions.py`（406 行）同时承担 Session、Run 和终态同步三种编排；
- 现有架构测试用线性层级表描述早期 CLI 项目，表达不了 ports/adapters 和独立性约束；
- 对未来 RAG/知识库/多通道没有任何声明的落位——不重构，这些能力进场时只会继续堆顶层包。

## 3. 设计原则

### 3.1 核心概念

- **AgentLoop**：ReAct 算法，负责模型轮次、工具反馈和继续/结束判断；
- **Run**：一次任务的状态、预算、暂停、恢复、checkpoint 和终态；
- **Session**：对话历史及多个 Run 的用户级容器；
- **Application**：创建 Runtime、Session、Run 并驱动 AgentLoop 的用例层——编排逻辑唯一所有者；
- **Execution**：Workspace、宿主进程、容器和取消控制；
- **Integration**：MCP、Skill、Web Access 及未来知识库等外部能力适配；
- **Contract**：API、CLI 和内部边界共享的稳定 DTO、事件和错误类型。

### 3.2 拆分标准

仅在存在明确独立变化原因、独立输入输出、独立生命周期或独立测试边界时拆分。以下情况保持同模块：

- 共同维护同一状态不变量；
- 修改时总是一起变化；
- 拆分后需要传递大量内部字段；
- 子文件只能命名为 `helpers/common/misc`；
- 只有一个调用点且没有独立领域含义。

文件行数不作为 CI 失败条件，不设新硬线。保留 **600 行非阻断预警**：文件首次超过 600 行时，
由人或 AI 对职责数量、共同状态不变量、依赖扇入/扇出、公共符号、测试定位和可抽离边界做具体
分析，在 `docs/ARCHITECTURE.md` 大模块评审表记录"保留"或"拆分"及理由。已评审文件行数再
增长约 20% 或新增独立职责时重新评审。复杂声明、状态机、配置模型允许超 600 行，前提是职责
单一、依赖清晰、测试可定位。

### 3.3 命名标准

- 保留 `agent/loop.py`，不使用 `engine.py` 隐藏 Agent 核心；
- 名称使用领域名词：`tool_batch.py` 优于 `execution.py`，`checkpoint.py` 优于 `recovery_codec.py`；
- `runtime` 一词只表示完整 Agent Runtime（归 application）；宿主/容器能力统一称 `execution`；
- `web_access` 表示联网抓取，避免与 Web UI/API 混淆；
- **顶层 `contracts/` 专指跨进程调用方可依赖的稳定公共 DTO；包内本地协议统一命名
  `ports.py`**（`providers/ports.py`、`tools/ports.py`），避免多处 `contracts.py` 混淆；
- 避免没有限定语的 `manager.py`、`service.py`、`base.py`、`extensions.py`；
- 包内实现允许私有，公共导入只通过明确的 `__init__.py`。

## 4. 目标目录

```text
src/assistant_agent/
├── contracts/                       # 稳定公共 DTO 与错误；全仓库最低层，零项目内依赖
│   ├── events.py                    # StepEvent、ToolDisplay、契约版本
│   ├── failures.py                  # RunFailure、FailureCode、BudgetSnapshot
│   ├── interactions.py              # Request/Decision DTO
│   ├── capabilities.py
│   └── errors.py                    # 公共 Service 异常
│
├── agent/                           # Agent 核心（零基础设施/厂商 SDK 耦合）
│   ├── loop.py                      # ReAct 主循环，必须显式可见
│   ├── turn.py                      # 单轮模型流与响应归一化（仅当能切出稳定接口才抽，不强拆）
│   ├── tool_batch.py                # 同轮工具批次和协议完整性（原 execution.py）
│   ├── prompts.py
│   ├── context/
│   │   ├── conversation.py
│   │   ├── window.py
│   │   └── compaction.py
│   └── run/
│       ├── state.py                 # Run 聚合与状态模型
│       ├── ports.py                 # CheckpointRepository/Telemetry/Control 端口
│       ├── coordinator.py           # 状态转换与不变量的唯一入口
│       ├── budgets.py               # 三类预算与 continuation
│       ├── recovery.py              # pause/resume/uncertain
│       └── checkpoint.py            # 编解码和 schema 迁移
│
├── application/                     # UI/传输无关的用例编排——编排唯一所有者
│   ├── runtime.py                   # AgentRuntime 生命周期
│   ├── runs.py                      # start/pause/cancel/resume
│   ├── sessions.py                  # create/load/list/delete/sync
│   ├── ports.py                     # RuntimeFactory/SessionRepository 等用例端口
│   ├── interactions.py              # InteractionPort 同步实现
│   └── capabilities.py
│
├── bootstrap/                       # 启动期装配（composition root）；非运行时调用层
│   ├── runtime.py
│   ├── service.py                   # 注入默认 factory 的公共 AgentService 组合适配
│   └── tools.py
│
├── providers/                       # 模型端口与适配；未来 embedding/rerank 端口也在此
│   ├── ports.py
│   └── litellm.py
│
├── tools/                           # 保持内置工具文件布局，重建执行边界
│   ├── models.py                    # ToolResult/ArtifactRef/ToolBudget 等纯数据
│   ├── ports.py                     # Tool/Workspace/Process/Artifact/Telemetry 协议
│   ├── context.py                   # ToolContext 运行时依赖载体，只依赖端口
│   ├── registry.py / permissions.py / policy.py / display.py / validation.py
│   └── （既有工具文件原位保留，不做 builtin/ 聚类）
│
├── integrations/                    # 外部能力适配；未来 knowledge/（RAG 连接器）与此平级
│   ├── mcp/
│   ├── skills/
│   └── web_access/                  # 原 web/
│
├── execution/                       # 原 runtime/：受控执行环境
├── persistence/                     # 原 session/ + ArtifactStore：sessions/runs/artifacts
├── observability/                   # 原 obs/：logger.py、redaction.py
│
├── service/
│   └── __init__.py                  # 纯稳定 re-export，可导入 contracts/application/bootstrap
│
├── ui/                              # 现状保留：CLI 终端展示；依赖规则钉死只被 cli/main 使用
├── cli/
├── config/
└── main.py                          # 兼容 console script，转发 CLI 入口
```

与 v1 的目录差异及理由：

1. **`service/` 只有 `__init__.py`，删除 facade.py**。真正编排在 application；完整资源装配在
   bootstrap。`service` 可纯 re-export `bootstrap.runtime.create_runtime` 和注入默认 factory 的
   `bootstrap.service.AgentService`，但自身无函数、类、状态转换或装配逻辑。
2. **`tools/` 不做 builtin/ 聚类，但必须拆解 `base.py`**。当前 base 同时拥有数据、协议、运行时
   Context 和具体默认依赖，只改名会制造伪 port；因此拆为 models/ports/context，既有工具文件原位。
3. **`ui/` 原位保留**，不并入 `cli/presentation/`。它已经是 CLI 展示层，改名是化妆；
   用依赖规则（只被 cli/main import）钉死语义即可。若未来真出现第二种 UI 再改不迟。
4. **`bootstrap/` 保留为独立小包**而不并入 application：composition root 必须能 import 一切
   具体实现，单独成包才能让 import-linter 用包级规则表达"唯一例外"，并入 application 反而
   要打文件级洞。它是启动期模块，不是运行时调用链上的一层。

不新增泛化的 `core/`、`common/` 或全局 `ports/`。

## 5. 依赖规则

允许的主方向：

```text
contracts <- agent <- application <- service / cli
                ^          |
providers/ports ┤          +-> application/ports
tools/ports ----+
ui <- cli / main（仅此二者可 import ui）
bootstrap -> 所有具体实现（唯一例外，负责装配）
integrations / execution / persistence / observability -> 消费方 ports
```

强制规则（全部由 import-linter 表达，例外必须带删除阶段）：

1. `contracts` 零项目内依赖；
2. `agent` 可依赖 Pydantic 等基础类型库，但不依赖 CLI、Service、LiteLLM、MCP SDK、具体 Store、
   Rich、Typer 或具体日志器；
3. `application` 只依赖 contracts、agent、providers/tools 端口和自身端口；不依赖具体 integrations、
   execution、persistence、observability，也不直接解析第三方异常；
4. `providers.litellm` 实现 provider 端口，Agent 不 import LiteLLM；
5. `integrations` 实现工具或应用端口，不决定 Run 终态；适配器可以依赖消费方定义的 port；
6. `execution` 可实现 `agent/run/ports.py` 与 `tools/ports.py`，但不依赖 AgentLoop、RunCoordinator、
   CLI 或状态转换实现；
7. `persistence` 实现 checkpoint/session/artifact repository 端口，只保存/加载，不决定状态转换；
8. **`service` 只允许 import `contracts`、`application` 和 `bootstrap`，且只做 re-export**——公共
   导出快照测试同时校验 `service/__init__.py` 内无函数/类定义；
9. `cli` 只调用 application/service，不导入 Agent 内部状态机；
10. `ui` 只被 `cli`/`main` import，自身只依赖 `contracts` 与展示所需的工具 display 类型；
11. `bootstrap` 是唯一可同时依赖具体 provider、MCP、Store、Execution 和 Tool 的位置；公共
    `create_runtime` 与默认 `AgentService` 组合入口在此定义，经 service 纯 re-export。

### 5.1 `agent/run/` 内部 DAG

包内单向依赖，禁止兄弟模块反向 import coordinator：

```text
state.py <- ports.py
state.py <- checkpoint.py
state.py <- budgets.py
state.py <- recovery.py
{state, ports, checkpoint, budgets, recovery} <- coordinator.py <- agent/loop.py
```

- `state.py` 只依赖 `contracts` 和 Python/Pydantic；
- `ports.py` 定义 CheckpointRepository、RunTelemetry、RunControlPort，不导入具体 adapter；
- `checkpoint.py` 只负责 state 的编解码、迁移和 repository 端口调用，不决定状态转换；
- `budgets.py`、`recovery.py` 只操作显式 state/端口，不 import coordinator；
- `coordinator.py` 是 Run 状态转换唯一入口，统一维护 checkpoint 顺序和不变量；
- `loop.py` 只调用 coordinator 公共方法，不直接改 RunState 字段。

### 5.2 装配与公共入口调用链

```text
assistant_agent.service（纯 re-export）
  -> bootstrap.runtime.create_runtime（具体资源装配）
  -> bootstrap.service.AgentService（为 application service 注入 runtime factory）
  -> application（唯一用例编排）
  -> agent + ports

integrations/execution/persistence/observability
  -> 实现 agent/application/tools/providers 定义的 ports
```

`application.sessions.AgentService` 必须显式接收 `RuntimeFactoryPort`。为保持现有
`from assistant_agent.service import AgentService; AgentService(config_path=...)` 调用兼容，
`bootstrap.service.AgentService` 只负责注入默认 `create_runtime` 后委托 application 实现；它是
composition adapter，不拥有第二套 Session/Run 编排。

### 5.3 端口清单与所有权

端口由消费方定义，具体适配器实现；不建立全局 `ports/` 垃圾桶：

| 端口 | 定义位置 | 主要实现 | 消费方 |
|---|---|---|---|
| `ModelProviderPort` | `providers/ports.py` | `providers/litellm.py` | AgentLoop/Context Compactor |
| `InteractionPort` | `contracts/interactions.py` | `application/interactions.py`、CLI adapter | Tool/Application recovery |
| `RunCheckpointRepository` | `agent/run/ports.py` | `persistence/runs.py` | RunCoordinator/checkpoint |
| `RunTelemetry` | `agent/run/ports.py` | `observability/logger.py` | RunCoordinator |
| `RunControlPort` | `agent/run/ports.py` | `execution/control.py` | AgentLoop/tool batch |
| `RuntimeFactoryPort` | `application/ports.py` | `bootstrap/runtime.py` | AgentService |
| `SessionRepository` | `application/ports.py` | `persistence/sessions.py` | Session use cases |
| `RunCatalogRepository` | `application/ports.py` | `persistence/runs.py` | list/delete/prune use cases |
| `ToolPort` | `tools/ports.py` | 内置工具、MCPTool、FunctionTool | ToolRegistry/AgentLoop |
| `WorkspacePort` | `tools/ports.py` | `execution/workspace.py` | 文件/搜索/进程工具 |
| `ProcessSupervisorPort` | `tools/ports.py` | `execution/process.py` | shell/git/MCP lifecycle |
| `ArtifactStorePort` | `tools/ports.py` | `persistence/artifacts.py` | 有界进程输出 |
| `ToolTelemetry` | `tools/ports.py` | `observability/logger.py` | ToolContext/Registry |

同一个具体 `persistence.runs.RunStore` 可以同时实现 `RunCheckpointRepository` 与
`RunCatalogRepository`，但两个消费方只看到各自所需的最小接口。端口不得返回具体 adapter 类型或
第三方 SDK 对象。

## 6. 逐模块迁移矩阵

| 当前模块 | 目标模块 | 处理方式 |
|---|---|---|
| `agent/loop.py` | `agent/loop.py` | 保留名称；移出单轮/批次/恢复驱动，不改事件顺序 |
| `agent/execution.py` | `agent/tool_batch.py` | 重命名，职责限定为工具批次 |
| `agent/loop_resume.py` | `agent/run/recovery.py` + `agent/tool_batch.py` | 按状态恢复与批次执行归属合并 |
| `agent/run_control.py` | `agent/run/coordinator.py` | 合并终止状态转换 |
| `agent/run_state.py` | `agent/run/state.py` | 迁移模型和 schema migration 入口 |
| `agent/recovery.py` | `agent/run/coordinator.py` + `recovery.py` | 状态转换与交互恢复分开 |
| `agent/recovery_codec.py` | `agent/run/checkpoint.py` | checkpoint 编解码归位 |
| `agent/recovery_definitions.py` | `agent/run/recovery.py` | 定义差异属于恢复兼容 |
| `agent/continuation.py` | `agent/run/budgets.py` | 预算状态和 continuation 同一所有者 |
| `runtime/control.py` 中 Agent 控制抽象 | `agent/run/ports.py` + `execution/control.py` | Agent 依赖 Control port；线程安全实现留 execution |
| `agent/events.py`、`failures.py` | `contracts/events.py`、`failures.py` | 公共契约迁出内部实现 |
| `agent/context.py`、`token_budget.py` | `agent/context/conversation.py`、`window.py` | 对话与窗口计算分离 |
| `agent/compaction.py` | `agent/context/compaction.py` | 原职责保留 |
| `interaction/models.py` | `contracts/interactions.py` | 公共 DTO 归一 |
| `interaction/ports.py` | `contracts/interactions.py` + `application/interactions.py` | InteractionPort 协议随契约；阻塞/安全默认实现归应用层 |
| `llm/client.py` | `providers/ports.py` + `litellm.py` | 模型协议与第三方适配分离 |
| `service/runtime.py` | `application/runtime.py` + `bootstrap/runtime.py` | AgentRuntime 生命周期归应用；create_runtime 具体装配归 bootstrap |
| `service/_runtime_builders.py` | `bootstrap/runtime.py`、`tools.py` | 装配集中到 composition root |
| `service/sessions.py` | `application/runs.py`、`sessions.py` + `bootstrap/service.py` | 编排归应用；兼容构造器只注入默认 factory |
| `service/events.py`、`capabilities.py`、`errors.py` | `contracts/` | 统一公共契约所有权 |
| `service/__init__.py` | `service/__init__.py` | 从 contracts/application/bootstrap 兼容 re-export；自身零定义 |
| `session/store.py`、`run_store.py` | `persistence/sessions.py`、`runs.py` | 明确为持久化适配 |
| `runtime/*` | `execution/*` | 消除 Runtime 术语冲突；实现 Run/Tool control、workspace、process ports |
| `obs/*` | `observability/*` | 使用完整名称 |
| `mcp/*` | `integrations/mcp/*` | MCP 外部适配归位 |
| `skills/*` | `integrations/skills/*` | Skill 集成归位 |
| `web/*` | `integrations/web_access/*` | 避免与 Web 产品混淆 |
| `tools/base.py` | `tools/models.py` + `ports.py` + `context.py` | 拆开纯数据、消费方协议和运行依赖载体，禁止只改名 |
| `tools/result.py` | `tools/models.py` | ToolResult/ArtifactRef 与预算等纯数据收敛 |
| `tools/artifacts.py` | `persistence/artifacts.py` | 实现 ArtifactStorePort，工具层不拥有文件持久化 |
| 其余 `tools/*` 工具文件 | **原位保留** | 不做 builtin/ 聚类（v1 范围，v2 砍掉） |
| `ui/*` | **原位保留** | 不并入 cli（v1 范围，v2 砍掉）；依赖规则钉死用途 |
| `cli/*`、`main.py` | 原位保留，仅入口转发收敛 | 不按用例重排命令文件 |
| `config/schema.py` | 先保留 | 不为行数拆；仅在独立变化原因明确时按配置领域拆分 |

## 7. 公共兼容策略

### 7.1 Python API

以下公共入口在 M19 全程保持可用：

```python
from assistant_agent.service import ...
from assistant_agent.interaction import ...
from assistant_agent.tools import ...
```

迁移期旧包只做显式 re-export，不保留业务实现。内部测试先迁移到新所有者路径，契约测试继续
验证旧公共入口。`assistant_agent.service` 可转发 bootstrap 的 composition 入口，但不自行装配。
M19 完成后是否废弃 `assistant_agent.interaction` 另立版本计划，本期不删除。

### 7.2 事件和 checkpoint

- 不改变 StepEvent 字段、kind、顺序和 contract v1；
- 不改变 Run checkpoint v3 文档结构和存储路径；
- 不改变 session/run/call ID 生成规则；
- 不改变 permission、tool_uncertain、continuation 和 session_synced 语义；
- 若重构中发现必须破坏以上任一项，立即停止对应阶段，单独出契约升级方案。

M19 预期为结构性兼容改造，结论应为"公共服务契约无变化"。每阶段仍按正式契约规则输出该结论
或明确变更清单；M19g 收尾输出可直接交给 API 项目 AI 的完整影响说明。

## 8. 测试与质量护栏

### 8.1 护栏调整

- 删除基于旧顶层包 rank 的线性层级表；
- 删除 300/500 行失败规则，改为 §3.2 的 600 行非阻断预警 + 大模块评审表；
- 不按测试文件行数强制拆分。

### 8.2 新护栏（M19a 全部先行落地，业务实现迁移在后）

1. **直接引入 `import-linter` 开发依赖**，用声明式 `forbidden/layers/independence`
   contracts 表达 §5 规则；每条规则与对应目标包在同一提交启用，不对不存在的包建立无效 contract；
2. **`contracts/` 零反向依赖护栏与包创建同一提交生效**，不允许先搬后补；
3. 项目内部 import cycle 检查；迁移期例外必须精确到 import 且标注删除阶段，禁止整包放行；
4. `service/__init__.py` 无定义检查（只准 re-export）；
5. `assistant_agent.service`、`interaction`、`tools` 公共导出快照测试；
6. StepEvent/Failure/Interaction 序列化契约测试；
7. Run 状态转换参数化测试；
8. checkpoint v1/v2/v3 fixture 迁移与 round-trip 测试；
9. CLI 和 Service 对同一 scripted provider 的事件序列一致性测试；
10. Ruff `C901` 函数复杂度检查，阈值按当前基线实测设定，此后只收紧不放宽；
11. 手写 AST 测试只保留 import-linter 表达不了的项目专属规则（业务 MCP 外置边界等）。

### 8.3 测试目录

**不做四象限重排。** 测试目录镜像源码结构，仅新增 `tests/contract/`（service 导出、事件、
checkpoint 快照）。源码 `git mv` 后对应测试文件同步 `git mv` 并更新 import，不在同一提交
重写断言；无需移动的测试保持原位，不为目录整齐制造无行为价值的 diff。

## 9. 未来能力落位图（企业级问答 Agent 演进）

**本期一行代码都不为以下能力预建。** 这张表的作用是验收目录设计：每项未来能力都有唯一
明确落位，架构才算合格；若某项找不到家，说明结构错了，现在改。

| 未来能力 | 落位 | 说明 |
|---|---|---|
| RAG 检索工具 | `tools/` 新文件（如 `retrieval.py`） | 实现现有工具协议、注册即用；不动内核 Loop |
| 知识库连接器（wiki/对象存储/向量库） | `integrations/knowledge/<backend>/` | 与 mcp/skills 平级的外部能力适配；重连接器可外置为 MCP server（走既有 D:\Dev\mcp 约定） |
| Embedding/Rerank 模型 | `providers/ports.py` 增端口 + 新适配器 | 复用"换模型只改 config.yaml"的既有卖点 |
| 文档摄取管线（切分/清洗/索引） | `application/` 新用例，或独立外置服务 | 编排属于 application；绝不进 `agent/` |
| 答案引用/溯源 DTO | `contracts/` additive 扩展 | 按契约规则升版本、写迁移说明 |
| 新通道（HTTP API / IM 机器人 / Web） | 外部项目或新顶层适配包 | 只 import `assistant_agent.service` / `contracts`，不依赖内部 application |
| 审计 / 多租户归属 | `observability/` + `persistence/` 加字段 | additive；权限钩子已在 tools/permissions |
| 子 Agent / 多 Agent 编排 | 另立里程碑 | 依赖 event 脊柱演进，M19 只保证不阻断 |
| event-sourcing 脊柱（状态由事件派生） | 另立里程碑 | M19 保证 StepEvent 是唯一事件出口、契约独立于实现，即为该方向留好接口 |

## 10. 文档和项目规则同步

实施阶段必须同步：

- `AGENTS.md` / `CLAUDE.md`：替换旧线性层级和行数铁律，写入新所有权、依赖规则和 600 行评审流程；
- `README.md`：更新当前架构、扩展点和公共入口；
- `DESIGN.md`：保留历史快照，加当前架构文档链接，不改写历史正文；
- 新增 `docs/ARCHITECTURE.md`：当前架构唯一事实源——上下文、依赖图、命名表、大模块评审表、
  未来能力落位图（§9 迁入并持续维护）；
- `ROADMAP.md`：增加 M19 阶段状态；
- `docs/TECH_DEBT.md`：关闭 D22，登记迁移中新发现的真实债；builtin 聚类、ui 合并、测试重排等
  主动放弃项不是技术债，放入 `docs/ARCHITECTURE.md` 观察信号表，出现真实维护成本后再立项；
- `docs/agent-service-integration-guide.md`：确认公共契约路径和导入保持不变；
- CI：架构测试换为 import-linter + 保留 AST 专属规则，不降低 pytest/Ruff/mypy/coverage/eval 门。

## 11. 实施阶段

七阶段，按依赖支点从外到内推进。M19a-c 未全绿不得修改 Loop；M19d 未全绿不得迁移 application；
每阶段独立验收、独立提交、独立输出公共契约结论。

### M19a：架构事实源与首批护栏

- 引入 import-linter 和 `docs/ARCHITECTURE.md`；
- 创建 `contracts/` 包，零项目内依赖 contract 与包同一提交生效；
- 保留业务 MCP 外置等项目专属 AST 测试；
- 移除 300/500 行失败规则，落地 600 行非阻断预警与大模块评审表；
- 建立现有公共导出、StepEvent、Interaction、checkpoint v1/v2/v3 基线快照；
- 不移动业务实现，不为尚不存在的目标包创建全局例外。

验收：594 基线测试及既有质量门全绿；新 contract 真正约束已存在的 contracts 包。

实施记录（2026-07-19）：公共服务契约无变化。依据：本阶段新增的 `contracts` 为空骨架，
只增加依赖护栏、现有 DTO/导出快照和架构文档；未修改 `assistant_agent.service` 公共出口、
StepEvent v1、Interaction、Run/Session 状态、checkpoint v3 或生命周期语义。

### M19b：稳定公共契约

- 迁移 StepEvent、ToolDisplay/ToolPreview DTO、RunFailure、Interaction DTO/Protocol、公共错误与能力 DTO；
- `contracts` 中只保留数据、枚举、Protocol 和安全校验，不迁入渲染、脱敏或状态转换行为；
- `agent/events.py`、`agent/failures.py`、`interaction/models.py`、`service/events.py` 等旧路径兼容转发；
- 启用 contracts 与原实现包之间的 forbidden contract。

验收：字段、默认值、序列化、sensitive 标记和公共 root 导出完全一致；contract version 仍为 1。

实施记录（2026-07-19）：已建立 `assistant_agent.contracts` 推荐公共入口；Service 既有导出、
DTO 字段/default/类型身份和 StepEvent v1 均保持兼容，旧 `agent.events`、`agent.failures`、
`interaction.models`、`service.events/capabilities/errors` 路径保留薄转发。该变化为向后兼容扩展，
不要求 API 立即修改导入；M19g 将同步正式契约中的推荐路径和迁移说明。

### M19c：Provider 与 Tool 边界

- 建立 `providers/ports.py` + `litellm.py`，原 `llm/client.py` 暂作兼容转发；
- 将 `tools/base.py` 拆为 `models.py`、`ports.py`、`context.py`，旧 base 暂作兼容转发；
- Interaction、Workspace、Process、Artifact、Telemetry、RunControl 在工具底层只以 Protocol 出现；
- 移除 ToolContext 对 NullLogger、ProcessSupervisor、RunControl、Workspace 具体默认类的 import；
- 启用 agent 禁止 LiteLLM、tools ports 禁止具体 adapter 的 import-linter contracts；
- 本阶段不修改 `agent/loop.py`，由旧路径 re-export 保持兼容。

验收：Provider 双后端测试、工具权限/预算/审计/Artifact 测试全绿；核心不再看到 LiteLLM 类型。

实施记录（2026-07-19）：Agent 已改为依赖 `ModelProviderPort`，LiteLLM 实现迁入
`providers/litellm.py`；summary provider 的创建移至装配层显式注入。工具结果/预算、基础设施
端口、运行上下文和 Tool 基类已分离，核心 `tools.context.ToolContext` 不再创建或导入具体
Logger/Workspace/RunControl/ArtifactStore。旧 `llm.client`、`tools.base/result` 保留兼容出口。
公共 Service/StepEvent/Interaction/checkpoint 契约无变化；AgentLoop 新增的 `summary_client`
仅为内部向后兼容可选参数，既有调用签名仍可用。

### M19d：Agent 核心收敛（**实施前需用户再次明确授权**）

- 建立 `agent/run/`（state/ports/coordinator/budgets/recovery/checkpoint）与 `agent/context/`；
- 保留 `agent/loop.py`，迁移到 Provider/Tool/Run ports；`execution.py` 改名 `tool_batch.py`；
- `turn.py` 仅在能切出稳定接口且不增加状态泄漏时创建；
- RunCoordinator 依赖 CheckpointRepository/RunTelemetry，不直接依赖 RunStore/NullLogger；
- AgentLoop 依赖 RunControlPort，不直接依赖 execution 具体实现；
- 不改变状态机、事件顺序、checkpoint schema、ID 和恢复语义。

前置条件（已核实满足，见文首）：M18 全部合入 main；若实施期间 budgets/continuation/recovery
出现未合并功能分支，本阶段冻结。改动前后跑全量测试、scripted eval、recovery eval，并对比事件序列。

实施记录（2026-07-19，已获用户授权）：已建立 `agent/context/`、`agent/run/`、`turn.py` 和
`tool_batch.py`，Loop 保留在 `agent/loop.py`。ControlState、RunControl、checkpoint repository 和
Run telemetry 均改为消费方端口；Agent 不再依赖 runtime/obs logger/session/具体 provider。
`_drive` C901 从 35 降至 27，项目基线同步收紧。迁移前后 scripted eval 均为 18/18 PASS，
tool calls 27→27、input/output tokens 120/31 保持不变；recovery eval 4/4 PASS；StepEvent v1、
checkpoint v3、ID、权限、continuation、tool_uncertain 和 session_synced 语义无变化。公共服务契约
无变化，旧 Agent 模块路径保留 identity-compatible 转发。

### M19e：Application、Bootstrap 与 Service

- 建立 `application/runtime.py/runs.py/sessions.py/interactions.py/capabilities.py/ports.py`；
- AgentService 显式接收 RuntimeFactoryPort，Session/Run 用例只依赖 repository/runtime ports；
- 建立 `bootstrap/runtime.py/tools.py/service.py`，集中具体资源装配；
- 保持公共 `AgentService(config_path=...)` 和 `create_runtime(...)` 签名，经 service 纯 re-export；
- `service/__init__.py` 只从 contracts/application/bootstrap 导出，无函数、类或状态转换；
- CLI 改用同一个 service/application 入口，移除 CLI 对 Agent 内部恢复模块的穿透。

验收：CLI/API 共用同一用例；公共构造签名、初始化回滚、close、Session 隔离和恢复行为不变。

实施记录（2026-07-19）：Runtime/Session/Run 用例已迁入 `application/`，具体装配集中到
`bootstrap/`；`service/__init__.py` 仅转发 contracts/application/bootstrap 的稳定入口，并由
AST 与 import-linter 双重约束。CLI 恢复命令不再穿透 Agent 状态机、Store 或日志 adapter，
有 Session 的 Run 复用 `SessionRuntime`，历史无 Session Run 通过 Application 兼容用例恢复。
Tool Interaction 脱敏改为由 composition root 注入，核心默认过度脱敏。公共 `AgentService`、
`create_runtime` 和 DTO 身份保持兼容，StepEvent v1/checkpoint v3 无变化；全量 604 passed / 5 skipped
（含本阶段新增 inspect/delete Run 用例），Ruff、mypy 和 8 条 import-linter contract 已通过。

### M19f：基础设施与集成命名迁移

- 使用纯 `git mv` 优先完成 `runtime/→execution/`、`session/→persistence/`、
  `obs/→observability/`、`mcp/skills/web→integrations/`；
- `tools/artifacts.py` 迁入 `persistence/artifacts.py` 并实现 ArtifactStorePort；
- 具体 execution/persistence/observability/integration 只实现消费方 ports，不反向拥有业务状态；
- 每类包移动与必要 import 调整分提交，审查 rename 识别，不夹带功能变化；
- 启用对应 independence/forbidden contracts，删除旧路径内部实现。

验收：MCP optional/required、Skill、Web、安全执行、Session/Run Store、Artifact 和日志测试全绿。

实施记录（2026-07-19）：宿主执行、持久化、日志与外部集成实现已分别迁至 `execution/`、
`persistence/`、`observability/` 和 `integrations/{mcp,skills,web_access}/`，ArtifactStore 归入
`persistence/`。源码和主体测试只使用新路径；旧 `runtime/session/obs/mcp/skills/web` 及
`tools.artifacts` 保留 identity-compatible 薄别名并由独立 contract 测试覆盖。新增 execution、
persistence、observability 独立性及 integrations 不拥有用例的护栏，import-linter 12/12 通过；
全量 606 passed / 5 skipped，Ruff、mypy 全绿。公共 Service DTO、StepEvent v1、checkpoint v3、
Session/Run 生命周期和 CLI 行为无变化。

### M19g：兼容层、文档与契约收尾

- 删除已无内部调用的临时兼容转发；保留文档承诺的 public root 导入；
- 测试仅按源码镜像做必要移动，并新增 `tests/contract/`，不做四象限重排；
- 更新 AGENTS、CLAUDE、README、DESIGN 链接、ROADMAP、TECH_DEBT、ARCHITECTURE；
- 更新正式 Agent Service 契约，预期结论为结构兼容、DTO 无变化；
- 输出可直接交给 API 项目 AI 的 commit、兼容影响和联调清单；
- 实测测试数、覆盖率、源码行数、eval 和大模块评审结果后归档 M19。

验收：全部 DoD、CI 矩阵和跨项目契约闭环完成，工作区无旧内部实现或无主模块。

实施记录（2026-07-19）：删除私有临时 `service._runtime_builders`，保留并测试历史公共根与
identity-compatible 旧路径；同步 AGENTS/CLAUDE/README/DESIGN/ROADMAP/TECH_DEBT、架构事实源、
正式 Service 契约和 API AI 交接。D11/D22 已还清，剩余 5 项技术债。公共契约结论：StepEvent v1、
checkpoint v3、Interaction/Run/Session/failure/生命周期语义无破坏；`RunExecution.warning` 是默认空串的
向后兼容扩展，API 无阻断性修改。最终验收：606 passed / 5 skipped、coverage 84%、Ruff/mypy、
12/12 import-linter、scripted 18/18、recovery 4/4 全绿；13974 行生产 Python、1366 行 eval，
无超过 600 行生产模块。

## 12. 提交策略

每阶段至少一个独立提交；纯移动提交（`git mv`）和逻辑调整提交必须分开，保留 Git 历史可读性：

1. `docs/test: define M19 architecture contracts and guardrails`
2. `refactor: establish stable public contracts`
3. `refactor: isolate provider and tool ports`
4. `refactor: consolidate agent run and context domains`
5. `refactor: extract application and composition roots`
6. `refactor: align infrastructure and integration packages`
7. `docs: finalize M19 architecture and service contract`

审查 `git diff --summary` 确认 rename 被正确识别；每次提交前审查 `git diff --cached`
排除密钥与垃圾文件。

## 13. 风险与控制

| 风险 | 控制 |
|---|---|
| 大量 import 变化导致隐藏回归 | 每阶段小步迁移，旧公共路径 re-export，立即跑全量测试 |
| checkpoint 类型路径变化影响反序列化 | checkpoint 只存 JSON，不持久化 Python 类型路径；真实 fixture round-trip |
| API 与 CLI 使用不同实现 | application 用例唯一，service 纯 re-export、cli 只做适配 |
| service 重新长出编排 | import-linter 限依赖 + `__init__.py` 无定义检查双保险 |
| Git rename 被识别为删除新增 | 纯移动与修改分提交，审查 `git diff --summary` |
| 为目标目录制造空抽象 | 仅迁移已有职责；§9 落位图只是地图，不预建接口 |
| M18 类功能改动与搬迁叠加 | M19d 冻结条款：相关文件存在未合并功能分支即停 |
| 行数硬线移除后文件失控 | 600 行非阻断评审表 + import contracts + C901 复杂度 + 循环检查 |
| 主动放弃项将来出现真实需求 | ARCHITECTURE 观察信号表记录触发条件；出现第二 UI、工具职责纠缠等事实后再转技术债或里程碑 |

## 14. 明确不做

- 不新增 Agent 功能；不预建 RAG/知识库/多通道的任何接口或空目录（§9 只是落位声明）；
- 不做 builtin 工具聚类、`ui/` 并入 cli、测试四象限重排；仅在 ARCHITECTURE 记录重新评估信号；
- 不改为全栈 async；
- 不增加子 Agent、远程 Workspace 或数据库；
- 不修改 assistant_agent_api / assistant_agent_web；
- 不改变权限默认值、沙箱默认值或 MCP 失败策略；
- 不借结构改造重写已验证的状态机；
- 不引入 event-sourcing 脊柱——属功能演进，另立里程碑；本期仅保证结构不阻断该方向；
- 不为追求"纯架构"引入依赖注入框架。

## 15. 完成定义

1. 目标目录（§4）和依赖规则（§5）落地，无临时双实现；
2. `agent/loop.py` 清晰可见且只负责 Agent 算法；
3. Run 状态、预算、恢复、checkpoint 收敛到 `agent/run/`，内部 DAG 达标；
4. Service/CLI 复用 application；`service/` 为纯 re-export 且公共导入兼容;
5. StepEvent v1、checkpoint v3、Session 数据和 MCP/Tool 名称完全兼容；
6. import-linter contracts、契约快照、状态机测试和跨入口一致性测试全部通过，例外清单清零
   或每条带明确删除计划；
7. `pytest`、coverage、Ruff format/check、mypy、scripted eval、recovery eval 全绿；
8. `docs/ARCHITECTURE.md`（含落位图与大模块评审表）、README、ROADMAP、TECH_DEBT、
   AGENTS、CLAUDE 和 API 正式契约同步；
9. 输出可直接交给 API 项目 AI 的契约影响说明（预期结论："公共服务契约无变化"+依据）；
10. 每阶段经过 diff、密钥、垃圾文件和公共契约审查后独立提交。
