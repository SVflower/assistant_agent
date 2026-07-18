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

## 2. 当前迁移状态

M19a 已建立 `contracts` 支点、契约基线和依赖护栏；M19b 已迁入稳定公共契约。
M19c-M19f 按依赖从内到外迁移。迁移期间旧路径仅作兼容转发，不允许形成双实现。
完整迁移矩阵见 `docs/m19-architecture-reconstruction-plan.md`。

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
- `cli` 通过 service/application 调用，不穿透 Agent 内部状态机。
- `tools` 不得反向依赖 Agent 或 UI。
- 业务 MCP server 始终外置，Agent 仓库只包含通用 client 与接入配置。

`agent/run/` 内部 DAG：

```text
state <- ports
state <- checkpoint
state <- budgets
state <- recovery
{state, ports, checkpoint, budgets, recovery} <- coordinator <- agent/loop
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
| 当前无超过 600 行的生产模块 | - | 2026-07-19 | 无需评审 | M19a 实测 | 首次超过 600 行 |

行数不是拆分判据，也没有硬失败线。复杂声明或内聚状态机允许超过预警线，但必须留下分析。

## 6. 复杂度基线

Ruff C901 是循环检查而非立即重写指标。M19a 以当前最高复杂度 35 为非回退基线；M19d 完成
Agent 核心收敛后按实测下调。高复杂度函数应结合状态不变量判断，不能为降低数字拆出隐式共享状态。

| 函数 | M19a 复杂度 | 处理阶段 |
|---|---:|---|
| `agent.loop.AgentLoop._drive` | 35 | M19d 复审 |
| `ui.conversation_renderer.ConversationRenderer.render` | 23 | 本期不移动 UI，观察 |
| `service.runtime.create_runtime` | 20 | M19e 装配收敛 |

## 7. 未来能力落位

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

## 8. 暂不改造项与观察信号

| 暂不改造 | 原因 | 重新评估信号 |
|---|---|---|
| 内置工具聚类到 `builtin/` | 只有目录 churn，没有独立生命周期 | 工具命名冲突或注册职责实际纠缠 |
| `ui/` 并入 `cli/` | 当前 UI 已是清晰终端展示层 | 出现第二种进程内 UI adapter |
| 测试四象限重排 | 与源码迁移叠加、收益低 | 测试归属长期无法定位 |
| 全栈 async | 同步线程模型已满足当前 Service 契约 | 并发测量证明同步边界成为瓶颈 |
