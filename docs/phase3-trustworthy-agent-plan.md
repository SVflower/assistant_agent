# 第三阶段规划：可信执行与质量闭环

> 状态：**实施中；M9a/M9b/M9c 已完成，下一项 M10a**。
> 本文基于第二阶段完成后的代码审计、240 个测试与 67% 覆盖率基线，以及 Claude Code、
> OpenAI Agents SDK、LangGraph 等成熟 Agent 的公开设计原则整理。它是第三阶段唯一总规划；
> 每个子里程碑开工前仍需按项目工作流细化为对应的 `docs/<里程碑>-plan.md`。

## 1. 阶段目标

第二阶段解决了“可观测、可扩展、可接生态、上下文可控”。第三阶段不继续堆功能，先把现有
Agent 从“可信用户本机上的功能型 Agent”提升为“边界明确、失败可恢复、行为可评测”的工程系统。

本阶段目标：

1. 把上下文、会话路径、生命周期清理等正确性要求变成不可突破的硬保证。
2. 把分散在工具里的确认逻辑收口为框架强制执行的权限策略，明确提示词不是安全边界。
3. 建立行为级 eval，能够比较模型、提示词、Skills 与 MCP 变更是否真的提升 Agent 完成率。
4. 让大文件、长输出、错误反馈和文件写入适合真实编码任务，而非只适合小型演示。
5. 增加步骤级 checkpoint，为崩溃恢复、审批恢复和未来异步运行时打基础。

## 2. 审计基线

截至 2026-07-14：

- `pytest -q`：240 passed。
- `pytest --cov -q`：总覆盖率 67%。
- 核心覆盖率：loop 91%、context 92%、compaction 91%、registry 91%。
- 关键缺口：`cli/setup.py` 0%、`main.py` 0%、`ui/console.py` 11%。
- `ruff check .` 通过；`ruff format --check .` 有 13 个文件不符合格式化结果。
- 已复现：配置窗口 100 时，单条超大消息可使估算占用达到 1029，超长摘要可达到 1046。
- 已复现：`SessionStore._path("../outside")` 可逃出 sessions 目录。
- 已复现：`/model` 切换后，默认 Compactor 仍持有旧 client。

## 3. 规划原则

- **先硬保证，后体验优化**：路径、窗口、权限、清理优先于新工具和多 Agent。
- **默认兼容，安全变化显式说明**：会增加确认次数的策略必须在配置和文档中写清迁移方式。
- **扩展点承担横切逻辑**：权限、参数校验、observer 不继续散落到每个 Tool 和 Loop 分支。
- **不为行数拆文件**：拆分必须带来职责边界、测试隔离或依赖稳定性收益。
- **异步化设决策门**：先解决可取消和资源有界问题，不为追求“现代”一次重写整个循环。
- **真实模型结果不进 CI 硬门槛**：CI 用确定性轨迹 eval；真实模型评测用于版本对比和发布记录。

## 4. 里程碑总览

| 里程碑 | 主题 | 优先级 | 是否动 `loop.py` | 阶段状态 |
|--------|------|:------:|:----------------:|----------|
| M9a | 硬正确性与工程基线 | P0 | 轻微，已授权 | ✅ 完成 |
| M9b | 统一权限与信任边界 | P0 | 原则上不动 | ✅ 完成 |
| M9c | Agent 行为 Eval 与 CI 质量闭环 | P0 | 不动 | ✅ 完成 |
| M10a | 工具契约与大文件/大输出工程 | P1 | 原则上不动 | 必做 |
| M10b | 步骤级 Checkpoint 与可恢复执行 | P1 | 实质改动，需要确认 | 必做 |
| M10c | 异步与可取消运行时 | P2 | 重构级，需要单独立项 | 决策门，不作为第三阶段退出条件 |

第三阶段完成定义：M9a、M9b、M9c、M10a、M10b 全部交付；M10c 只完成可行性评估，
是否实施由真实的取消/并发需求决定。

## 5. 目标架构

```text
main / cli
    |
    v
Runtime -------------------------- RunObserver / CheckpointStore
    |                                      ^
    v                                      |
AgentLoop ---- Conversation / TokenBudget -+
    |
    v
ToolRegistry ---- ArgumentValidator ---- PermissionPolicy
    |                                      |
    +---- built-in tools                   +---- allow / ask / deny
    +---- Skills                           +---- workspace / network / process
    +---- MCP
```

建议新增或抽取的模块：

- `agent/events.py`：`StepEvent` / `EventKind`，让 UI 不再为事件类型依赖完整 Loop。
- `agent/token_budget.py`：消息、工具 schema、摘要、输出预留和最终 envelope 校验。
- `tools/policy.py`：权限请求、规则匹配、allow/ask/deny 决策。
- `tools/validation.py`：统一参数校验与稳定错误反馈。
- `ui/stream_renderer.py`：把流式渲染状态机从 Console 输入/确认逻辑中分离。
- `session/checkpoint.py`：步骤级 RunState/checkpoint；与聊天历史存储职责分开。
- `mcp/transport.py`：隔离 MCP SDK transport API 变化。

暂不拆：`config/schema.py`、`session/store.py` 的会话模型、`obs/logger.py`。这些文件当前仍内聚，
应先通过新增行为验证是否真的需要拆。

## 6. M9a：硬正确性与工程基线

### 解决什么

- 单条消息或摘要仍可突破上下文窗口。
- Session ID 可路径穿越，会话覆盖写非原子。
- MCP/Runtime 部分启动失败可能不清理已打开资源。
- `/model` 后默认摘要器与 Session 元数据可能仍指向旧模型。
- 格式化、类型检查、CI 与声明的开发命令不一致。

### 必做

1. `ContextEnvelope` 最终校验：任何请求都不得超过配置窗口；超大用户消息给明确错误或受控截断，
   工具结果和摘要按各自策略裁剪。
2. 抽 `TokenEstimator` 接口：优先模型感知计数，失败回退保守估算；不让 context 反向依赖 LiteLLM。
3. `summary_max_tokens`/摘要长度硬限制；压缩后再次验证 envelope。
4. Session ID 限定稳定格式并做目录 confinement；保存采用临时文件 + `os.replace()` 原子替换。
5. `_connect_one()`、`build_runtime()` 增加失败回滚，确保 transport/session/logger 均被清理。
6. `AgentLoop.set_client()` 同步更新“跟随当前模型”的 Compactor；显式 summary provider 不受影响。
7. `/model` 更新 Session 元数据并记录 `model_switch`；resume 时明确“沿用当前配置”或“恢复原模型”，
   二选一写成稳定语义，不能继续保存但不使用。
8. `summary_model` 必须引用已存在 provider；校验 reserved/system/tools 至少留出可用消息空间。
9. 将 `ruff format --check .`、类型检查和 Python 3.11+ CI 矩阵加入质量基线。
10. volatile 依赖增加兼容上界，至少 `mcp>=1.28,<2`；不在本期引入新的包管理器。

### 不做

- 不实现权限系统、沙箱、步骤级 checkpoint。
- 不重写 AgentLoop，只修复 client/compactor 状态同步。
- 不追求所有 provider 的精确 tokenizer；保证“估算失败也不越配置硬线”。

### 内核边界

需要轻微修改 `agent/loop.py` 的 `set_client` 与 token budget 接线。开工前必须向用户确认。

### 测试与验收

- 单条消息、工具 schema、摘要分别超限时，最终 envelope 仍不超过窗口。
- checkpoint 损坏、游标越界、summary_model 不存在时给稳定错误，不崩溃或静默回退。
- `../`、绝对路径、分隔符变体不能逃出 sessions 目录。
- 模拟写入中断后旧会话文件仍可加载。
- MCP initialize 与 Runtime 构造任一步失败，已打开资源全部关闭。
- `/model` 后主调用与默认摘要调用都使用新 client。
- Windows + Linux CI、Python 3.11/3.13 至少覆盖一组；pytest、coverage、format、lint、type check 全绿。

## 7. M9b：统一权限与信任边界

### 解决什么

当前确认由 Tool 自觉调用，Shell 正则可被等价命令绕过；提示词还错误声称联网/安装依赖会被系统
自动拦截。项目级 Skills 和 MCP 元数据也属于未信任指令来源。

### 必做

1. 在 Registry 执行前强制经过 `PermissionPolicy`，Tool 只声明动作，不自行决定是否询问。
2. 定义 `PermissionRequest`：tool、capability、目标路径/域名/命令、风险说明、稳定 category。
3. 权限顺序固定为 `deny -> ask -> allow`；支持会话级“永久允许”和配置级规则。
4. 提供模式：`readonly`、`workspace`（推荐默认）、`strict`、`unrestricted`（显式危险）。
5. 工作区外读写都需要策略判断；敏感目录可 deny；网络出口单独 capability。
6. Shell 默认只放行经过验证的只读命令集合，其余 ask；正则只用于风险提示，不能作为安全证明。
7. MCP 确认展示脱敏后的关键参数，而不是只显示工具名；`auto_approve` 文档标明是信任整个 server。
8. 项目级 Skill 默认标记未信任：首次启用确认或配置显式 trust；个人 Skill 与项目 Skill 分来源展示。
9. 修正系统提示词和 README：明确权限由框架执行，当前没有 OS 级真沙箱。
10. 增加 `PreToolUse`/`PostToolUse` observer 接缝，为未来 hooks/guardrails 留稳定扩展点。

### 不做

- 不在本期实现 Windows/macOS/Linux 全平台 OS 沙箱。
- 不做自动 prompt-injection 分类器；信任来源、权限和最小暴露优先。
- 不加入组织级远程策略中心。

### 内核边界

权限在 `ToolRegistry`/`ToolContext`/Tool 元数据层完成，原则上不修改 `agent/loop.py`。

### 测试与验收

- Python、PowerShell、重定向、curl/pip 等等价写入/联网方式不能绕过策略。
- 区外读、区外写、网络、进程执行分别有独立规则和审计事件。
- deny 永远优先于 ask/allow；“永久允许”不跨 capability 或目标范围扩散。
- 不可信 Skill 不会静默注入系统提示词；MCP 参数确认已脱敏。
- 未启用真沙箱时 UI 明确显示当前保护等级，不制造虚假安全感。

## 8. M9c：Agent 行为 Eval 与 CI 质量闭环

### 解决什么

单元测试能证明组件工作，但不能回答“模型是否选对工具、是否越权、是否完成任务、Skills/MCP 是否
真的有用”。没有行为 eval，后续改 prompt、模型和工具都无法量化回归。

### 必做

1. 新增 `evals/`：案例格式包含 task、fixture workspace、允许工具、禁止工具、预算、结果断言。
2. CI 确定性 runner：使用 scripted/fake client 检查轨迹、权限、预算、终止与恢复协议。
3. 真实模型 runner：手动或定时运行，输出 JSONL/Markdown 报告，不作为普通 PR 硬门槛。
4. 最小基准集覆盖：读取分析、单文件编辑、多文件修改、测试修复、权限拒绝、坏工具参数、
   上下文压缩、MCP/Skill 触发、预算耗尽。
5. 指标：任务成功率、非法工具率、工具调用数、重复调用率、token、耗时、用户确认次数。
6. A/B 维度：provider、prompt 版本、Skill 开关、MCP 开关、compaction 开关。
7. CI 加 `ruff format --check`、`ruff check`、type check、pytest 与架构测试。
8. 为 `cli/setup.py`、Runtime 失败清理和关键 Console 状态补集成测试，结束 D5 的高风险部分。

### 不做

- 不用 LLM-as-judge 作为唯一评分。
- 不在 CI 调真实付费模型。
- 不追求大型公开 benchmark；先覆盖本项目的真实工作流。

### 内核边界

不修改 `agent/loop.py`；eval 通过公开事件流和 fixture client 驱动现有 Agent。

### 验收

- 至少 12 个确定性案例，能捕获越权、重复工具、预算和协议回归。
- 至少 5 个真实编码任务可对两个 provider 生成可比较报告。
- prompt/工具变更后可回答“成功率、调用数、token 是否变好”。

## 9. M10a：工具契约与大文件/大输出工程

### 解决什么

`read_file` 无分页，截断后无法按范围重试；参数 schema 只给模型看，不在运行时统一验证；Shell/Git
先把全部输出读进内存再截断；文件工具的“原子”只保证替换逻辑，不保证磁盘写入原子。

### 必做

1. `read_file` 支持 `start_line/end_line` 或 `offset/limit`，返回范围、总行数和下一页提示。
2. `code_search` 支持上下文行；Git/Shell 大结果支持分页或 artifact 句柄。
3. 工具输出在来源端限量，不能等完整内容进入内存后才由 Registry 截断。
4. 文件写/edit/multi_edit 统一走原子写帮助函数，尽量保留换行风格。
5. Tool 参数统一校验；错误返回稳定 `code/message/retryable`，同时保持给模型的文本可读。
6. 扩展 `ToolResult`：稳定错误码、metadata、artifact refs；兼容旧 `output/is_error`。
7. MCP `structuredContent` 转换为结构化 metadata 或 JSON 文本，不再成功却返回“无内容”。
8. 按职责拆 `file_ops.py` 为读/浏览与写/编辑模块；共享路径、编码和原子写帮助函数。

### 不做

- 不增加 git commit/push 等写操作。
- 不实现完整二进制编辑器或 IDE 协议。
- 不在本期异步化 AgentLoop。

### 内核边界

通过 Tool/Registry 扩展完成，原则上不修改 `agent/loop.py`。

### 验收

- 能稳定读取和编辑 10 万行文件的任意区域，不依赖整文件进入上下文。
- 无限输出命令不会让进程内存无界增长。
- 所有内置工具对错误参数给一致、可重试的反馈。
- 文件写入中断不会留下半文件。

## 10. M10b：步骤级 Checkpoint 与可恢复执行

### 解决什么

当前只在完整任务结束后保存聊天历史。进程在工具副作用之后崩溃，会丢失轨迹并可能在恢复后重复执行。
审批、预算、重复检测和待执行工具批次也无法恢复。

### 必做

1. 定义可序列化 `RunState`：conversation/checkpoint、iteration、工具预算、重复签名、当前阶段。
2. 每个模型轮次和工具结果后写 checkpoint；聊天存档与运行 checkpoint 分文件/职责。
3. 工具调用记录状态 `planned -> started -> completed/failed`，调用 ID 稳定。
4. 恢复时遇到 `started` 但无结果的副作用工具，必须询问用户，绝不自动重放。
5. 审批前保存 pending request；进程退出后可恢复并继续，而不是重新规划整轮。
6. checkpoint 使用原子写、版本号与迁移；损坏时回退最近有效版本。
7. 通过 observer/checkpoint 接口接入 Loop，避免把存储细节写进控制流。
8. 日志 trace_id/session_id/run_id 语义对齐，补齐 model switch 与恢复事件。

### 不做

- 不做分布式任务队列、多进程 worker、云端状态服务。
- 不承诺任意外部工具 exactly-once；只保证不确定状态不会被框架静默重放。
- 不做 rewind/time travel UI。

### 内核边界

这是对 `agent/loop.py` 的实质改动，必须单独出 M10b 详细方案并经用户确认后实施。

### 验收

- 在模型调用后、工具执行前、工具执行后分别模拟崩溃，均可恢复到明确状态。
- 已完成工具不重复；状态不确定的副作用工具必须用户决定。
- 工具预算、重复熔断和 compaction checkpoint 恢复后语义不变。
- 旧 Session 文件仍能载入，checkpoint 缺失时按普通聊天恢复。

## 11. M10c 决策门：异步与可取消运行时

MCP 的线程桥、阻塞 Shell、无法中途取消工具和只读工具不能并行，说明同步 Tool 协议存在上限。但把
整个 Agent 改成 async 的回归面很大，因此第三阶段只做可行性评估。

触发实施的信号：

- Ctrl+C 无法停止长工具成为高频问题。
- 同一轮多个只读工具顺序执行造成明显延迟。
- MCP/HTTP 工具数量增加，线程桥故障或清理复杂度持续上升。
- 需要作为库或服务嵌入已有 asyncio 应用。

若触发，优先采用兼容迁移：`Tool.run_async()` 默认在线程池调用旧 `run()`，内置工具逐步原生异步；
AgentLoop 提供 async 核心，CLI 再包同步入口。禁止一次性重写全部工具。

## 12. 跨里程碑风险与边界

- **权限变严格会增加提示次数**：必须提供规则迁移、会话记忆和清晰风险说明，避免确认疲劳。
- **token 计数更准确可能改变截断行为**：保留兼容测试，但正确性优先于“逐字节等于旧错误行为”。
- **checkpoint 会增加磁盘写入**：采用批次边界、原子替换和可配置保留策略，避免每 token 写盘。
- **结构化 ToolResult 会影响大量测试**：先兼容旧字段，再逐工具迁移。
- **真实模型 eval 有随机性**：保存模型、配置、prompt hash、工具集和运行轨迹，不把单次结果当结论。
- **真沙箱仍不在本阶段承诺**：M9b 提供强制权限策略，但对不可信代码的硬隔离仍需容器/WSL2/VM。

## 13. 实施顺序与审阅门

1. 用户审阅本文，确认第三阶段范围和优先级。
2. 为 M9a 写详细实施计划；因轻触 `loop.py`，开工前再次获得明确授权。
3. M9a DoD 全绿并归档后，依次进入 M9b、M9c、M10a。
4. M10b 开工前重新评估 AgentLoop 结构，并单独获得内核修改授权。
5. M10c 只在触发信号成立时立项，不因“路线图有这一项”自动实施。

每个里程碑继续执行项目 DoD：测试/覆盖率、format/lint/type check、技术债、密钥检查、状态文档同步、
真实冒烟或如实记录外部阻塞。完成后计划移入 `docs/archive/phase3/`。

## 14. 第三阶段明确不做

- 子 Agent / 多 Agent 编排。
- Web GUI。
- 向量数据库与跨会话 RAG 记忆。
- 自动 git commit/push。
- 全平台 OS 沙箱统一实现。
- 为拆文件而拆文件。

这些方向不是永久拒绝，而是必须建立在权限、eval、可恢复执行和稳定工具契约之上。
