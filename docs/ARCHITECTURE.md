# Assistant Agent 架构

> 本文是当前架构、依赖方向和模块所有权的唯一事实源。里程碑方案解释迁移过程，
> 本文描述合入主线后必须长期成立的规则。

## 1. 架构目标

Assistant Agent 是可嵌入 CLI、API 和其他通道的本地优先任务 Agent。架构采用
Ports and Adapters：稳定契约与 Agent 领域位于内侧，外部 provider、MCP、文件系统、
进程、持久化和终端展示位于边缘，`bootstrap` 是唯一 composition root。

长期目标依赖方向：

```text
contracts <- agent <- application <- service / cli
                ^          |
providers/ports ┤          +-> application/ports
tools/ports ----+

bootstrap -> concrete providers / tools / integrations / execution / persistence / observability
```

核心原则：

1. `contracts` 是跨进程公共 DTO、事件和错误的唯一所有者，零项目内依赖。
2. `agent` 拥有 ReAct Loop、上下文和 Run 状态机，不依赖 UI 或具体基础设施。
3. `application` 拥有 Session/Run 用例编排，只依赖领域对象和消费方端口。
4. `bootstrap` 负责具体资源装配，是允许同时看到内外层实现的唯一位置。
5. `service` 仅稳定 re-export，不拥有编排、状态转换或资源装配。
6. 外部调用方只依赖 `assistant_agent.service` 和 `assistant_agent.contracts`。

## 2. 当前目录与所有权

M19 已完成架构迁移和开发期清理。生产包只保留目标结构，不维护迁移前的内部 import 路径，
也不通过 `sys.modules`、动态别名或薄转发隐藏第二套目录。完整迁移记录见
`docs/archive/phase12/m19-architecture-reconstruction-plan.md`。

```text
src/assistant_agent/
├── contracts/       # 稳定公共 DTO、事件、错误与 Interaction Protocol
├── agent/           # ReAct 内核、上下文与 Run 状态机
│   ├── loop.py
│   ├── context/
│   └── run/
├── application/     # Runtime、Session、Run 用例编排
├── bootstrap/       # 唯一 composition root
├── providers/       # 模型端口与 LiteLLM adapter
├── tools/           # 工具模型、端口、上下文、Registry 与内置工具
├── integrations/    # MCP、Skills、Web Access adapter
├── execution/       # Workspace、进程监管与 RunControl adapter
├── persistence/     # Session、Run checkpoint 与 Artifact 存储
├── observability/   # 日志、审计与脱敏
├── service/         # 稳定进程内服务根入口，仅 re-export
├── interaction/     # 稳定同步交互实现根入口
├── cli/             # 命令与终端输入适配
├── ui/              # Rich 展示
├── config/          # 配置 schema、加载和路径
└── main.py          # CLI 入口
```

目标所有权：

| 概念 | 唯一所有者 | 不应出现的位置 |
|---|---|---|
| 公共事件、失败、Interaction DTO | `contracts/` | `agent/`、`service/` 的实现文件 |
| ReAct 算法 | `agent/loop.py` | `application/`、CLI |
| Run 状态、预算、恢复、checkpoint | `agent/run/` | Service、Store adapter |
| Conversation、窗口与压缩 | `agent/context/` | CLI、Provider adapter |
| Session/Run 用例编排 | `application/` | Service、Persistence |
| 资源装配 | `bootstrap/` | CLI setup、Application |
| 模型协议与实现 | `providers/ports.py`、`providers/litellm.py` | Agent 内的厂商判断 |
| 工具数据、协议、上下文 | `tools/models.py`、`ports.py`、`context.py` | 单体 `base.py` |
| Workspace/进程/控制实现 | `execution/` | Agent 状态机 |
| Session/Run/Artifact 保存 | `persistence/` | Tools、Application 内部 |
| MCP/Skill/Web Access | `integrations/` | Agent 核心 |
| 日志与脱敏 adapter | `observability/` | Contracts |

## 3. 强制依赖规则

规则由 `.importlinter` 随目标包逐阶段启用，项目专属规则保留在
`tests/test_architecture.py`。

- `contracts` 不得导入任何 `assistant_agent` 其他包。
- `agent` 不得导入 Rich、Typer、CLI、Service、LiteLLM、MCP SDK 或具体 Store。
- `application` 不得依赖具体 integrations、execution、persistence、observability。
- `service/__init__.py` 不得定义函数或类，只能从 contracts/application/bootstrap 转发。
- `cli` 通过 service/application 调用，不穿透 Agent 内部状态机；历史无 Session Run 的恢复也由
  Application 兼容用例拥有。
- `tools` 不得反向依赖 Agent 或 UI。
- 业务 MCP server 始终外置，Agent 仓库只包含通用 client 与接入配置。
- 禁止恢复 `llm/mcp/obs/runtime/session/skills/web` 等迁移前顶层包，以及
  Agent/Tool/Service/Interaction 的旧转发文件；架构测试直接检查这些路径不存在。

`agent/run/` 内部 DAG：

```text
state <- ports
state <- checkpoint
state <- budgets
state <- recovery
{state, ports, checkpoint, budgets, recovery} <- coordinator <- {resume, agent/loop}
```

`checkpoint` 只负责编解码、迁移和 repository 调用；状态转换统一由 coordinator 维护。

## 4. 命名与抽离规则

- `loop.py` 明确保留，核心算法不能被含糊的 `engine.py` 隐藏。
- 顶层 `contracts/` 专指稳定公共 DTO；包内消费方协议统一叫 `ports.py`。
- `runtime` 只表示完整 Agent Runtime；宿主、容器和进程能力称 `execution`。
- 联网抓取称 `web_access`，避免与 Web 产品混淆。
- 禁止新增无领域含义的 `common.py`、`misc.py`、`helpers.py` 或全局 ports 包。

只有出现独立变化原因、输入输出、生命周期或测试边界时才拆分。共同维护同一状态不变量、
修改总是同步、拆后需暴露大量内部字段的代码应保持内聚。

## 5. 大模块评审

生产 Python 文件超过 600 行时，测试只发出非阻断预警。首次触发必须评审职责数量、共同状态
不变量、依赖扇入/扇出、公共符号、测试定位和可抽离边界，并在下表记录决定。已评审文件增长
约 20% 或新增独立职责时重新评审。

| 模块 | 行数 | 触发日期 | 结论 | 理由 | 复审信号 |
|---|---:|---|---|---|---|
| `integrations/mcp/manager.py` | 720 | 2026-07-19 | 暂不拆分 | event loop 线程、连接表、惰性连接、后台目录发现、Runtime 工具可见性与关闭共同维护同一 server 生命周期；此时拆成多个有状态 owner 会增加竞态和清理遗漏。纯数据模型与目录持久化已分别抽到 `models.py`、`catalog.py` | 增加独立健康熔断职责；增长超过约 20%；或能以无共享可变状态的 port 分离连接 owner 与目录发现 owner |

行数不是拆分判据，也没有硬失败线。复杂声明或内聚状态机允许超过预警线，但必须留下分析。

## 6. 扩展启动生命周期

M20 将核心 Runtime 与可选外部扩展解耦，且不改变 Agent Loop 的工具定义冻结规则：

1. `bootstrap.create_runtime` 是启动阶段事件、Skill 元数据发现和 MCP 装配的唯一入口。
2. Skill 启动期只读取有界元数据；完整 `SKILL.md` 继续由 `load_skill` 按需载入。
3. `required` MCP 在 Runtime 创建期同步连接和发现，失败导致创建失败并回滚。
4. 有有效工具目录的 `optional` MCP 只注册稳定 Schema，首次调用时才连接。
5. 无工具目录的 `optional` MCP 在后台隔离发现，目录只对下一 Runtime 生效，不修改当前 Registry。
6. MCP 的 configured、catalogued、connected 是不同事实；工具目录不代表 server 已在线。
7. Runtime 关闭统一取消后台发现并关闭已建立连接，调用方不得另建 MCP 生命周期。
8. `inspect_runtime` 只读当前 Registry、可见 Skill 与 MCP capability，是模型能力自省事实源；不得搜索
   项目结构猜测自身能力。
9. 核心工具优先占用 Schema 预算；optional MCP 只使用剩余空间，被省略时产生结构化 notice，不能让
   整个 Runtime 因外部扩展过多而启动失败。

工具目录属于用户状态缓存，位于 workspace 状态命名空间下，不进入源码、Session history 或模型上下文；
仅保存配置指纹和脱敏 Tool Schema，不保存展开后的 env/header、调用参数、输出或第三方异常。

## 7. 受管命令生命周期

M21 维持同步 Agent Loop，但对进程执行建立完整所有权：

1. `execution/process.py` 拥有前台命令的 spawn、deadline、双流排空和进程树清理；deadline 结束后不得
   出现无界 `wait()` 或 reader `join()`。
2. 外层 Shell 先退出但后代仍持有 PIPE 时，终止原因为 `background_process`；受管树被清理，
   `run_shell` 返回 `background_process_detected`，不把等待伪装成模型思考。
3. `execution/jobs.py` 是后台进程状态和句柄的唯一 owner。每个 AgentRuntime 各有一个 registry，
   使用 opaque process ID，不跨 Runtime 共享。
4. `tools/processes.py` 只把 start/status/logs/stop/list 适配到 Registry、权限和 ToolResult；不拥有
   subprocess，也不向模型暴露 OS PID。
5. `bootstrap` 只装配一个进程管理器并同时注入 ToolContext/AgentRuntime；初始化失败和 Runtime close
   均幂等清理。
6. 后台输出按 stdout/stderr 分别有界保留；配置和公共事件不包含完整命令、环境变量或原始异常。
7. container Workspace 暂不注册可执行的跨步骤后台语义，避免 `docker exec` 客户端退出后容器内进程
   失去可证明所有权；调用返回结构化 unsupported，而不是退回宿主执行。

前台进程监管与后台进程 registry 复用 `ManagedProcessHandle`、Windows Job Object 和 POSIX process
group，不允许形成第二套平台终止实现。

## 8. Presentation Artifact 所有权

M24 不新增独立 Artifact 状态机：`contracts/charts.py` 拥有稳定 DTO 与 canonical 编码；
`tools/charts.py` 只负责声明式输入适配；`agent/run/coordinator.py` 是 Run 限额、冲突、幂等和 checkpoint
顺序的唯一 owner；Session 终态同步由 Application 编排，既有 RunStore/SessionStore 提供原子持久化。

完整图表可受 512 KiB/16 个/2 MiB 硬限内联保存，API 只经 Service 读取，不扫描持久化目录。
`present_chart` 依赖 checkpoint 正确性，因此 recovery 关闭时不注册；工具 schema 超出上下文预算时也
安全省略并发布 Runtime notice。两种降级都不得阻止 Runtime ready。

事件仍为 `tool_call -> tool_result(chart) -> final -> run_terminal`，Event contract v1 additive 兼容；
Run checkpoint v4 负责从 v1-v3 迁移空 presentations。M24 未修改 Loop，也未新增超过 600 行模块；
既有 `integrations/mcp/manager.py` 720 行评审结论不变，其状态/连接/目录发现共享同一生命周期，暂不
为行数机械拆分。

### 8.1 Web Runtime 部署边界

M25 继续以 `bootstrap.runtime` 作为唯一 composition root。`RuntimePolicy` 是可信调用方注入且 frozen 的
部署上限：`cli` 保持本机完整工具与交互审批；`web` 使用最终工具 allowlist，并把同一 profile 写入
`RuntimeCapabilities`。模型提示、工具参数、config.yaml 和浏览器请求都不能切换或放宽 profile。

Web allowlist 在注册阶段执行，覆盖内置、Skill、Web、展示、扩展和 MCP 所有注册入口。未注册工具既不
进入 schema，也不能按名称调用。Web 只自动允许 allowlist 中的受控工具；显式 deny 仍可收紧。当前
网络白名单仅含 `web_search`，`fetch_url` 因 DNS 校验结果尚未绑定实际连接而 fail closed。

服务器 Workspace 与内部 ArtifactStore 仍是 Runtime 实现资源，不等于模型能力。Web 没有文件、Shell、
Git、进程或扩展管理工具；当前唯一可下载语义是 M24 Chart Artifact。未来 Export Store 必须是独立
受管 adapter，以 opaque ref 暴露，不能重新注册通用文件写入。

Interaction 的有界等待仍由公共 Blocking port 单一拥有。入队时生成 `expires_at`；timeout、close、
pause/cancel interrupt 和异常均默认拒绝。Application 只负责在 Session 控制动作中唤醒 port，不复制
Interaction 状态机。M25 未修改 Loop，Event v1 和 checkpoint v4 不变。

## 9. 复杂度基线

Ruff C901 是循环检查而非机械拆分指标。M19a 的最高复杂度基线为 35；M19d 提取单轮模型流后
已按实测收紧到 27。高复杂度函数应结合状态不变量判断，不能为降低数字拆出隐式共享状态。

| 函数 | M19a 复杂度 | 处理阶段 |
|---|---:|---|
| `agent.loop.AgentLoop._drive` | 35 -> 27 | 已提取 `agent/turn.py`，事件顺序不变 |
| `ui.conversation_renderer.ConversationRenderer.render` | 23 | 本期不移动 UI，观察 |
| `bootstrap.runtime.create_runtime` | 20 | M19e 已收敛为唯一 composition root |

## 10. 未来能力落位

| 能力 | 预期位置 | 进入条件 |
|---|---|---|
| Embedding / Rerank provider | `providers/` 新端口与 adapter | 确定首个知识库用例后 |
| RAG/知识库连接器 | `integrations/knowledge/` | 至少一个真实外部数据源 |
| 文档摄取、切分与索引用例 | `application/` 或独立服务 | 数据规模和部署边界明确后 |
| 引用/溯源 DTO | `contracts/` additive 扩展 | 正式 API 契约设计完成后 |
| HTTP/Web/IM 通道 | 外部项目或明确 adapter 包 | 只经 service/contracts 接入 |
| 审计与多租户归属 | `observability/` + `persistence/` | 账号与租户模型确定后 |
| 子 Agent / 多 Agent 编排 | 独立里程碑 | 单 Agent 事件和恢复语义稳定后 |
| Event-sourcing 状态脊柱 | 独立里程碑 | 有可验证的重放/审计需求后 |

这些条目是落位地图，不授权预建空抽象。

## 11. 暂不改造项与观察信号

| 暂不改造 | 原因 | 重新评估信号 |
|---|---|---|
| 内置工具聚类到 `builtin/` | 只有目录 churn，没有独立生命周期 | 工具命名冲突或注册职责实际纠缠 |
| `ui/` 并入 `cli/` | 当前 UI 已是清晰终端展示层 | 出现第二种进程内 UI adapter |
| 测试四象限重排 | 与源码迁移叠加、收益低 | 测试归属长期无法定位 |
| 全栈 async | 同步线程模型已满足当前 Service 契约 | 并发测量证明同步边界成为瓶颈 |
