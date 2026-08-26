# M6.5 — 运行时预算与工具协议完整性

> 状态：已完成
> 归属阶段：第二阶段，M6 可观测性之后、M7 skill/MCP 之前
> 最后更新：2026-07-13
> 内核影响：**会修改 `agent/loop.py`；用户确认本方案后才允许实施**

## 1. 目标

在不更换现有框架的前提下，为单个任务增加明确的工具资源边界，并保证预算耗尽、单轮多工具调用、用户中断等终止场景始终产生合法的 OpenAI tool-call 消息序列。

本里程碑完成后应满足：

- 单个工具结果、单任务工具调用总数、单任务累计工具结果均有边界。
- 模型一次返回多个工具调用时，预算不足不会留下没有对应结果的 tool call。
- Registry 继续是无任务状态的工具目录；任务预算状态不进入 Registry 生命周期。
- 预算耗尽可诊断、可审计，并能被 fake client 稳定回归测试。
- 为 M7 接入外部 skill/MCP 工具提供统一的资源保护边界。

## 2. 当前基线与已确认问题

### 2.1 已有能力

- `AgentLoop` 已有 `max_iterations`、重复调用熔断、用户中断和用尽轮数续跑。
- `ToolRegistry.execute` 是所有内置工具的统一执行入口。
- `Conversation` 已按消息数和近似 token 预算截断历史。
- 一期已加入单次工具输出截断、递归日志脱敏和确认等待计时。

### 2.2 一期需要先收口的偏差

当前提交 `c1070a6` 与最终评审意见仍有差异，必须作为本里程碑 S0 先处理：

1. `max_tool_output_chars` 位于 `AgentConfig`，但它实际属于工具执行策略。
2. 默认值直接设为 8000。当前 system prompt 约 1919 字符、工具 schema 约 3454 字符，固定输入已约 5373 字符；对默认 8000 token 的本地窗口而言，单次再放入 8000 字符并不保守。
3. 确认等待只记录最后一次，不支持一个工具未来触发多次确认时的累计。
4. 日志只有扣除等待后的 `duration_ms`，缺少完整墙钟耗时和返回给模型的输出长度。
5. 当前 Windows/PowerShell 环境运行全量测试为 `163 passed, 2 failed`；失败测试使用 Unix `rm`，与项目宣称的 Windows 原生支持不一致。Ruff 全绿。

### 2.3 第二期核心缺口

- `max_iterations` 限制模型轮数，不限制一轮中模型返回多少个 tool calls。
- 单次输出虽有限制，但一个任务可以跨轮累计大量工具结果并写入会话存档。
- 预算若在一批 tool calls 中途耗尽，不能简单 break；assistant 已声明的每个 tool call 都必须得到对应 tool result。

## 3. 设计决策

### 3.1 预算归属

采用以下职责划分：

```text
AgentConfig                 定义任务级预算上限
ToolsConfig                 定义单次工具执行策略
AgentLoop.run()             创建、重置和终止任务预算生命周期
ToolContext                 持有本次任务的可变预算状态
ToolRegistry.execute()      检查/消费预算，执行工具并返回归一结果
EventLogger                 记录工具和预算事件
```

不采用旧计划的 `ToolRegistry.begin_task()/end_task()`。Registry 应保持为无任务状态的工具集合，避免未来 MCP、多会话或并发执行时共享计数器。

### 3.2 配置

```yaml
agent:
  max_iterations: 25
  max_tool_calls: 50
  max_total_tool_output_chars: 50000

tools:
  max_output_chars: 4000
```

- `tools.max_output_chars`：单次工具结果上限。默认从 8000 收紧到 4000；允许设为 0 关闭截断。
- `agent.max_tool_calls`：单任务允许消费的工具调用额度。未知工具会消费额度；超过上限后未执行的调用不再增加 `used_calls`，而由审计字段 `skipped_calls` 单独统计。
- `agent.max_total_tool_output_chars`：单任务实际写入 Conversation 的工具执行结果累计字符数；普通错误结果计入。框架为补齐协议生成的“预算耗尽”控制结果不计入，否则剩余为 0 时无法为未执行调用补 result。允许设为 0 表示不设累计上限。
- `max_iterations` 仍只代表模型轮数，不改变现有语义。
- chat 中每次新的用户任务调用 `run(task)` 时重置任务预算；`continue_check` 扩展轮数时不重置预算。

默认值是保守起点，不声称适合所有模型。验收时用 fake client 做确定性边界测试，并用本地 LM Studio 做读大文件、长 shell、代码搜索三类冒烟；若实测明显误伤，再调整配置默认值并记录依据。

### 3.3 配置迁移

一期已经公开了 `agent.max_tool_output_chars`。迁移到 `tools.max_output_chars` 时采用兼容加载：

- 新配置只写 `tools.max_output_chars`。
- loader 在原始 YAML 中发现旧字段且新字段未设置时，将旧值迁移到新字段。
- 两者同时出现时以新字段为准，并保持确定性。
- 旧字段不继续出现在 `AgentConfig` 公共模型中，避免形成长期双源。
- 增加迁移测试，README/config.example 只展示新字段。

### 3.4 预算状态

在 `tools/base.py` 增加独立 dataclass（暂名 `ToolBudget`）：

```text
max_calls
max_total_output_chars
used_calls
used_output_chars
exhausted_reason
```

`ToolContext` 持有 `budget: ToolBudget | None`，并提供明确方法重置/消费确认等待时间，Registry 不直接写私有字段。

预算检查分两步：

1. 执行前消费一次调用额度。没有额度时不执行工具，返回预算错误结果。
2. 执行后将结果限制在“单次上限”和“本任务剩余累计上限”两者的较小值，并消费实际返回字符数。

### 3.5 单轮多工具调用与协议完整性

当模型一轮返回 N 个 tool calls 时，Loop 仍先写入完整 assistant tool_calls 消息。随后必须为这 N 个调用逐个写入 tool result：

- 有预算：正常执行。
- 调用预算已耗尽：不执行，写入结构化错误结果。
- 累计输出预算已耗尽：不再执行后续工具，写入结构化错误结果。
- 一个调用执行后刚好耗尽输出预算：保留该调用被截断后的结果，其余调用写入跳过结果。

整批结果补齐后，Loop 发出 `budget_exhausted` 错误事件并结束任务。不得在批次中途直接 return，不得留下悬空 tool call，也不额外调用模型“尝试总结”，以保持终止语义与当前 `max_iterations` 一致。

预算错误结果使用稳定、可测试的文本格式，例如：

```text
未执行工具：任务工具调用预算已耗尽（50/50）。请缩小任务范围或提高 agent.max_tool_calls。
```

### 3.6 计时与审计收口

确认等待改为一次工具执行内累计，日志字段定义：

- `wall_duration_ms`：完整 `tool.run()` 墙钟耗时。
- `approval_wait_ms`：本次执行内所有确认回调累计等待。
- `execution_duration_ms`：`max(0, wall - approval)`。
- `duration_ms`：暂时保留，值等于 `execution_duration_ms`，作为兼容别名。
- `output_len`：工具原始输出长度。
- `returned_output_len`：实际返回 UI/Conversation 的长度。
- `truncated`：是否因单次或累计预算发生截断。

新增 `budget_exhausted` 事件：

```json
{
  "type": "budget_exhausted",
  "reason": "max_tool_calls",
  "limit": 50,
  "used": 50,
  "skipped_calls": 3
}
```

日志仍遵守本地、脱敏、载荷截断、写入失败非致命原则。

## 4. 实施范围与顺序

### S0 — 收口一期与恢复基线（不动 loop）

- 将单次输出配置迁移到 `ToolsConfig.max_output_chars`，加入旧字段兼容迁移。
- 默认值调整为 4000，并同步 `config.example.yaml`、README 和测试。
- 确认等待改为累计，并通过 ToolContext 方法 reset/consume，避免 Registry 直写私有字段。
- 补齐计时和输出长度日志字段。
- 修复 Windows 下依赖 `rm` 的两项测试，使测试在 Windows/Linux 上表达同一行为。
- 验收：`pytest`、`ruff` 全绿后才进入 S1。

### S1 — 预算数据模型（不动 loop）

- 新增 `ToolBudget` 与配置字段。
- ToolContext 支持安装/清理任务预算。
- Registry 支持调用额度检查、输出额度截断和预算错误结果。
- 单测覆盖预算对象和 Registry，不接 Loop。

### S2 — Loop 生命周期与批次终止（动内核）

- `AgentLoop.run()` 开始时创建新预算，结束/异常/中断时清理。
- 执行完整工具批次，为每个 call 写入 result。
- 若预算耗尽，在批次结果完整后发 `ItemEvent(kind="error")` 并终止。
- 不改变流式模型接口、重复调用熔断、continue_check、会话导出和 UI 依赖方向。

### S3 — 可观测性与文档

- EventLogger/NullLogger 增加预算事件和完整耗时字段。
- `/context` 可选显示本任务预算只留后续；本期不扩 UI。
- 更新 DESIGN、ROADMAP、config.example、TECH_DEBT。

### S4 — 集成测试与冒烟

- fake client 覆盖所有预算终止路径。
- 本地 LM Studio 做三类冒烟；若本地服务不可用，明确记录未执行，不伪造结果。
- 完整 DoD 验收。

实施结果：fake client 与全量测试通过；本地 LM Studio `/v1/models` 探测到已加载模型，
但首次生成请求返回连接错误，随后端口拒绝连接。该项按外部推理服务退出记录为未完成，
未伪造冒烟结果，也不影响确定性测试与架构验收。

## 5. 文件影响

| 文件 | 变更 | 动内核 |
|------|------|:---:|
| `config/schema.py`、`config/loader.py` | 新配置与旧字段迁移 | 否 |
| `tools/base.py` | ToolBudget、确认等待累计、ToolContext 生命周期 | 否 |
| `tools/registry.py` | 预算检查、结果截断、元数据 | 否 |
| `obs/logger.py` | 完整计时、输出长度、budget_exhausted | 否 |
| `agent/loop.py` | 每任务预算生命周期、批次完整终止 | **是** |
| `main.py` | 新配置注入路径 | 否 |
| `tests/test_config.py` | 配置与迁移 | 否 |
| `tests/test_tools.py` | 跨平台确认测试、Registry 预算 | 否 |
| `tests/test_obs.py` | 日志字段与预算事件 | 否 |
| `tests/test_loop.py` | 单轮/跨轮预算与协议完整性 | **是** |
| 文档与配置示例 | 状态、使用方式、归档引用 | 否 |

## 6. 测试计划

### 配置

- 新默认值、合法覆盖、负值拒绝、0 表示关闭累计/单次限制。
- 旧字段自动迁移；新旧并存时新字段优先。

### ToolContext / Registry

- 确认等待多次累计、无确认归零、相邻调用不串值。
- 未达预算正常执行。
- 调用预算耗尽时工具实现不被调用。
- 未知工具也消费调用预算并得到对应错误结果。
- 单次上限、累计剩余额度分别截断；原始/返回长度准确。
- 日志关闭或写失败不影响预算行为。

### AgentLoop

- 单轮工具数超过剩余额度：每个 call 都有 result，超额调用未执行。
- 跨轮累计调用数达到上限后终止。
- 累计输出额度在批次中耗尽：当前结果截断、后续调用跳过。
- 预算耗尽后不再请求模型，不留下悬空 tool call。
- 新一次 `run(task)` 预算重置；chat 历史保留但预算不继承。
- `continue_check` 扩展迭代次数时预算不重置。
- 用户中断、模型流错误、重复调用熔断路径无回归。

### 全量

```bash
pytest
pytest --cov
ruff format .
ruff check .
git diff --check
```

## 7. 验收标准

1. Windows 当前环境和项目支持的平台上，测试不依赖错误的 shell 方言；全量 pytest 绿。
2. `tools.max_output_chars` 成为单次工具输出的唯一新配置入口，旧配置可兼容迁移。
3. 日志能区分完整耗时、确认等待、近似执行耗时，并记录原始/返回输出长度。
4. 单任务工具调用总数和累计返回结果均受配置约束。
5. 任意单轮多工具批次中，每个 assistant tool call 都有对应 tool result。
6. 预算耗尽后产生清晰 ItemEvent 和结构化日志，未授权/超预算工具不执行。
7. Registry 不保存任务计数，工具/obs 不反向依赖 agent/UI，架构测试通过。
8. `agent/loop.py` 的改动只涉及预算生命周期和批次终止，不夹带 UI/provider/MCP 逻辑。
9. 技术债、ROADMAP、配置示例和归档引用与实现一致，无密钥或运行产物入库。

## 8. 明确不做

- 不在本里程碑实现 MCP、skill、网络工具或多 Agent。
- 不引入 LangChain/LangGraph 或新的状态机框架。
- 不实现真实模型在线 eval runner；继续使用 fake client 做确定性行为测试。
- 不做任务总 wall-clock timeout；shell 等具体工具继续使用自己的 timeout。
- 不改变 context 摘要/压缩策略，留给 M8。
- 不新增自动重试；有副作用工具不能在缺少幂等性声明时通用重试。

## 9. 风险与边界

- 工具输出预算是在工具执行后才能知道，当前工具的副作用可能已经发生；预算只能限制返回内容和后续调用，不能回滚已发生副作用。
- 固定字符数不是精确 token 数，但与项目当前字符估算策略一致，且配置可按模型调整。
- 将超额调用写为错误 result 会增添少量上下文，但这是维持 tool-call 协议完整性的必要成本。
- 预算状态放在复用的 ToolContext 中，当前循环为串行执行；未来并行任务需要把上下文改为任务独立实例，本里程碑不提前设计并发。

## 10. 文档归档规则

本项目沿用“实施计划完成后归档”的现有方式：

1. 审阅和实施期间，本文件保留在 `docs/m6_5-runtime-budget-plan.md`，作为本里程碑唯一执行依据。
2. M6.5 完成并通过 DoD 后，新建/使用 `docs/archive/phase2/`，将以下已完成方案移入：
   - `docs/m6-observability-plan.md`
   - `docs/quality-guardrails-plan.md`
   - `docs/quality-guardrails-final-plan.md`
   - `docs/m6_5-runtime-budget-plan.md`
3. 更新 AGENTS、ROADMAP、TECH_DEBT 和方案间的相对链接，确保没有失效引用。
4. ROADMAP 记录 M6.5 完成状态和实测结果；TECH_DEBT 只保留仍未解决的债务。
5. 归档移动与代码实现放在同一里程碑收尾提交中，提交前检查 `git diff --cached`，避免密钥和运行产物入库。

## 11. 审阅闸门

用户确认本方案后，按 S0 → S1 → S2 → S3 → S4 实施。S0/S1 完成并测试通过后，才进入 `agent/loop.py` 的 S2；若前置设计在测试中被证明不成立，先更新本方案并重新说明，不带着未解决偏差修改内核。
