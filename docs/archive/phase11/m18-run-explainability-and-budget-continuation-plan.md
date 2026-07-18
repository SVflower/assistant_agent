# M18 运行可解释性、预算恢复与结构化失败方案

## 背景

生产联调已证明 M17 的 `final` / `run_terminal` 边界有效，但预算耗尽等失败仍只以文本上报。
API 无法稳定判断失败原因、可执行动作及安全用量，也无法让交互式 Run 在工具预算耗尽前由用户决定是否继续。

M18 在现有 Agent Service 边界上补齐结构化失败、预算 continuation 和安全运行反馈，不在 API、
Web 或 CLI 中复制执行状态机。

## 目标

1. 为事件和 Run checkpoint 提供稳定、脱敏、可序列化的失败事实。
2. 统一 iteration、tool call、tool output 三类 continuation 的交互和恢复语义。
3. 提供不含隐藏推理的运行阶段和预算快照。
4. 将 Provider、工具、权限和依赖异常映射为稳定机器码。
5. 保持 M17 终态规则和已有 CLI/Service 行为兼容。

## 范围

### 必做

- 新增公共 `RunFailure`、`FailureCode`、`AllowedAction`、`BudgetSnapshot` 契约。
- `StepEvent` 向后兼容增加 `failure`、`phase`、`budget` 字段及 `activity` 事件。
- RunState schema v3 保存 failure、预算硬上限、扩展次数和已应用 continuation 决策。
- 扩展现有 `ContinueRequest`，统一三类预算资源；安全默认仍为 stop。
- 工具预算在执行下一调用前请求 continuation，确认后只扩展当前 Run，并先 checkpoint 后执行。
- Provider 429、临时不可用、超时和永久/内部错误分类，禁止透传第三方原始异常。
- 工具错误和未知副作用分类；未知副作用继续走既有 recovery Interaction。
- 公共 Service 输出安全阶段事实，并在失败 `run_terminal` 携带结构化 failure。
- 更新示例配置、配置校验、状态文档和 API 交接文档。

### 不做

- 不修改 `assistant_agent_api`、`assistant_agent_web`。
- 不新增 HTTP/WebSocket DTO、事件序号、重连缓存或用户权限。
- 不自动重试未知副作用工具，不暴露 chain-of-thought、密钥、环境变量或原始异常。
- 不新建第二套 Run、continuation 或 checkpoint 状态机。
- 不把所有普通工具失败升级为 Run 终止；可纠正的工具错误仍回喂模型。

## 现状评审与核心决策

### 状态机唯一性

- `AgentLoop` 仍是执行循环和预算边界的唯一所有者。
- `RunCoordinator` 仍是 RunState 转换与原子 checkpoint 的唯一所有者。
- `InteractionPort.confirm_continue()` 仍是 continuation 唯一交互入口。
- `SessionRuntime._stream()` 仍是 `run_terminal` 唯一生成点，且每个 Run 只生成一次。
- API 只消费 `StepEvent`、Interaction DTO 和 Run snapshot，不解析日志、异常或中文文本。

### 是否修改内核

需要修改 `src/assistant_agent/agent/loop.py`，原因如下：

- 工具预算耗尽当前在 Loop 内直接终止，continuation 必须在下一工具调用前介入并复用批次游标。
- Provider 流错误、上下文错误和 iteration 终止均在 Loop 的异常/终止边界产生。
- `preparing_context`、`calling_model`、`executing_tool` 等事实只有 Loop 能按真实顺序发出。

修改范围限于预算预检、结构化失败构造和安全 activity 事件；不改变 ReAct 主流程所有权，
不把交互或终态迁移到 UI/Service。按仓库铁律，实施前需用户明确授权本次内核修改。

## 公共失败契约

建议在 `agent/failures.py` 定义不可变、可 JSON 序列化的 DTO：

```text
RunFailure
  code: FailureCode
  safe_message: str
  retryable: bool
  allowed_actions: tuple[AllowedAction, ...]
  resource: BudgetResource | None
  used: int | None
  limit: int | None
  terminal_status: TerminalStatus
  phase: ActivityPhase
  unknown_side_effect: bool
```

稳定 code：

- `tool_output_budget_exhausted`
- `tool_call_budget_exhausted`
- `iteration_limit_reached`
- `context_limit_exceeded`
- `provider_rate_limited`
- `provider_unavailable`
- `provider_timeout`
- `tool_failed`
- `permission_denied`
- `dependency_unavailable`
- `internal_error`

合法 action：

- `continue`
- `stop`
- `resume_run`
- `retry_run`
- `start_new_run`
- `adjust_configuration`
- `inspect_dependency`
- `resolve_uncertain_tool`

action 由 Agent 按当前状态明确给出，不由 API 从 `safe_message` 推断。所有构造函数只接受预定义安全文本；
第三方异常仅用于本地日志，不能进入公共 DTO。

## 事件与终态

`StepEvent` 新增向后兼容可选字段：

- `failure: RunFailure | None`
- `phase: ActivityPhase | None`
- `budget: BudgetSnapshot | None`

新增 `kind="activity"`，phase 包括：

- `preparing_context`
- `calling_model`
- `executing_tool`
- `waiting_interaction`
- `saving_checkpoint`
- `syncing_session`

activity 可携带工具名、脱敏 `ToolDisplay` 和预算快照，但不得携带 reasoning 摘要。`reasoning` 继续标记
为 sensitive。新增字段和事件种类不改变既有字段含义，因此保留 contract version 1；若实施中发现必须修改
既有字段语义，再提升版本并记录迁移要求。

终态规则保持：

1. `final` 只承载完整 assistant 正文，不改变 RunState。
2. partial content 后失败不补发伪 `final`。
3. `completed`、`failed`、`paused`、`cancelled` 只由唯一 `run_terminal` 表达。
4. failed 的 `run_terminal` 必须携带 `RunFailure`；其他终态 failure 为空。

## 预算 continuation

### 统一语义

将现有 `ContinueRequest` 扩展为：

```text
reason/resource, used, limit, suggested_increment,
hard_limit, extension_count, max_extensions, legal_options
```

保留 `iterations_used` / `iteration_limit` 兼容属性或迁移别名，避免 CLI 和 API 立即破坏。

- interactive Run：达到 iteration 边界或下一工具调用将超过 call/output 预算时请求 continuation。
- non-interactive Run、超时、断线、端口关闭、异常、错误 request ID：默认 stop。
- continue 只增加当前 Run 对应预算，不修改 `AppConfig`。
- 每类资源独立统计扩展次数，语义一致但不共用额度。
- 扩展受 `hard_limit` 和 `max_extensions` 双重限制。
- extension 决策生成稳定 ID；`RunCoordinator.extend_budget()` 原子写入后才允许继续。
- 恢复时读取已增加 limit 和已应用决策 ID，不能重复扩展。

### 批次执行

工具预算必须在调用前预检。已完成 call 继续由 `RunCoordinator.result_for(call_id)` 跳过；预算确认后从当前
未完成 call 继续，不重放同批次前面的调用。若用户 stop，则当前及后续未执行 call 以安全 skipped result
收束，再写 failed terminal，保证 RunState 不保留悬空工具调用。

工具输出长度只有执行后才能确定：Registry 继续执行单工具输出截断和累计计量；若本次结果使累计预算达到
上限，已完成结果正常 checkpoint，下一调用前触发 continuation。若单次结果被累计预算截断，则先保存该结果，
再请求 continuation；不得自动重跑可能有副作用的工具。

### checkpoint

RunState v3 新增：

- `failure`
- iteration/tool call/tool output 的 `hard_limit`、`extension_count`、`max_extensions`
- `continuation_decisions`，保存 request/decision/resource/old limit/new limit

v2 -> v3 迁移以当前 limit 作为初始 limit，按配置计算硬上限，扩展次数为 0，failure 为空。未知未来版本
继续拒绝。恢复必须以 checkpoint limit 为准，不能用新配置缩回或重复增加已确认额度。

## Provider 与工具错误分类

LLM 层新增不含原始异常文本的 `ProviderFailure`：

- LiteLLM 429 / status 429 -> `provider_rate_limited`，可重试。
- Timeout / APITimeout -> `provider_timeout`，可重试。
- 临时 5xx、连接失败 -> `provider_unavailable`，可重试。
- 认证、模型不存在、参数/配置错误 -> `internal_error` 或启动期配置错误，不标记自动可重试。

原始异常仅经现有 logger 脱敏后写本地诊断。Loop 将 ProviderFailure 映射为公共 RunFailure。

工具层复用 `ToolResult.code/retryable/executed`：

- 普通可纠正错误产生带安全 failure 摘要的 `tool_result`，Run 可继续。
- permission code 映射 `permission_denied`。
- MCP/外部依赖不可达映射 `dependency_unavailable`。
- `mcp_outcome_unknown` 映射 `tool_failed + unknown_side_effect=true`，并进入既有
  `tool_uncertain` recovery Interaction；禁止自动重试。
- checkpoint/内部不变量错误映射 `internal_error`，公共事件不含异常、参数和环境信息。

## 配置策略

不直接修改本机 `config.yaml`。

- 保持 schema 的保守默认上下文，避免对未知本地模型虚报 32K 能力。
- `config.example.yaml` 提供服务联调推荐值：iterations 25、tool calls 60、tool output 80000、
  context 32000、reserved output 4096、history 60、tool output 4000、shell timeout 120。
- Provider 增加显式 `context_window` 能力声明；无法可靠自动推断的 provider 必须配置。
- 校验 `agent.max_context_tokens <= active_provider.context_window`，并校验 reserved output 与模型输出
  配置兼容；不兼容时启动失败，不静默截断。
- 新增 continuation 配置采用保守 schema 默认，并在示例中给出联调值及理由。

## 预计修改文件

核心：

- `src/assistant_agent/agent/failures.py`（新增）
- `src/assistant_agent/agent/events.py`
- `src/assistant_agent/agent/run_state.py`
- `src/assistant_agent/agent/recovery.py`
- `src/assistant_agent/agent/execution.py`
- `src/assistant_agent/agent/loop.py`（需用户授权）
- `src/assistant_agent/llm/client.py`
- `src/assistant_agent/interaction/models.py`
- `src/assistant_agent/service/events.py`
- `src/assistant_agent/service/runtime.py`
- `src/assistant_agent/service/sessions.py`
- `src/assistant_agent/config/schema.py`
- `src/assistant_agent/config/loader.py`（若跨字段校验不在 schema 内）
- `config.example.yaml`

测试：

- `tests/test_failures.py`（新增）
- `tests/test_loop.py`
- `tests/test_loop_recovery.py`
- `tests/test_run_state.py`
- `tests/test_llm.py`
- `tests/test_interaction_port.py`
- `tests/test_agent_service.py`
- `tests/test_service_contract.py`
- `tests/test_config.py`
- `tests/test_cli_*.py` 中受 continuation 展示影响的既有用例

文档和状态：

- `docs/m18-agent-api-handoff.md`（完成后新增）
- `docs/TECH_DEBT.md`
- `ROADMAP.md`
- `README.md`
- `AGENTS.md`
- `CLAUDE.md`
- 完成后把本方案和交接资料归档到下一阶段目录。

实际修改文件以实现依赖和测试结果为准，不做无关重构。

## 测试计划

确定性测试至少覆盖：

1. tool output 预算耗尽 stop 和 continue。
2. tool call budget continuation。
3. continuation 超时、关闭、异常、错误 ID、重复响应均 stop。
4. 每资源扩展次数和硬上限。
5. checkpoint 恢复不重复扩展、不重放已完成工具。
6. Provider 429、503、timeout、永久配置错误分类。
7. 普通工具失败、权限失败、依赖失败和未知副作用分类。
8. `final -> run_terminal(completed)` 顺序。
9. partial/error -> 唯一 `run_terminal(failed)`，不产生伪 final。
10. failure/activity/interaction DTO 不泄漏异常、密钥、环境变量、原始敏感参数。
11. CLI 与 Service continuation 和终态语义一致。
12. RunState v2 -> v3 迁移及未来版本拒绝。

质量门：

```bash
ruff format .
ruff check .
mypy src
pytest -q
pytest --cov
```

并单独确认 `tests/test_architecture.py`、Service integration 和恢复测试全绿。

## 验收标准

- API 无需解析错误 message，即可得到 code、动作、终态、资源用量与限制。
- 三类预算都能安全 stop；交互式确认后仅扩展当前 Run，并跨恢复保持。
- `run_terminal` 每个 Run 恰好一次，failed 必带结构化 failure。
- Provider 和工具第三方异常不进入公共 DTO。
- activity 不包含隐藏推理。
- M17 终态、pause/cancel/resume、权限和 checkpoint 顺序不回退。
- CLI、Service、恢复和架构测试全部通过。
- API 交接文档包含完整 DTO、枚举、迁移规则和场景事件序列。

## 风险与控制

- **批次重放风险**：只用现有 call_id/result checkpoint 跳过已完成调用，预算确认前不开始下一调用。
- **副作用未知**：继续使用 `tool_uncertain` recovery，不因 `retryable` 推断自动重试。
- **旧 checkpoint 兼容**：显式 v2 -> v3 migration，保留旧 limit，迁移测试锁定。
- **事件重复**：`run_terminal` 仍由 SessionRuntime 单点追加，Loop 不生成它。
- **信息泄漏**：失败使用白名单安全文本和数值字段，原始异常只进受控本地日志。
- **配置虚报**：模型上下文采用显式能力声明并在启动时拒绝不兼容组合。
