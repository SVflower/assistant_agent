# M20 CLI 启动与扩展生命周期方案

> 状态：已确认实施（用户要求“做出完整计划，并实施开发”）
> 日期：2026-07-19
> 内核影响：**不修改 `agent/loop.py`**
> 公共契约：向后兼容扩展，`EVENT_CONTRACT_VERSION` 保持 1

## 1. 背景与问题

当前 `chat -> build_runtime -> create_runtime -> start_mcp -> AgentLoop -> banner` 把 MCP
`spawn / initialize / tools/list` 放进 CLI 首屏和 Session 创建的关键路径。MCP server 即使配置为
`startup: optional`，也只是失败后允许降级，仍会同步阻塞到最慢 server 完成或超时。

本机基线中 Python/CLI 冷导入约 1.46 秒，完整 Runtime 约 9.98 秒；Playwright MCP 单独约
4.75 秒，Maxwell MCP 单独约 2.68 秒。模型尚未被调用，主要耗时来自本地 MCP 子进程、协议握手
和工具发现。`npx -y @playwright/mcp@latest` 还叠加网络、版本检查、缓存和杀毒扫描波动。

Skill 当前已经采用渐进披露：启动只扫描元数据，正文由 `load_skill` 按需读取；但元数据清单尚无
明确上下文预算。MCP 配置、安装、连接和工具可用性也尚未形成独立生命周期。

## 2. 调研结论

参考 Codex CLI、Claude Code 和 OpenHands 的公开文档、源码与问题记录，保留以下原则：

1. CLI 外壳和启动状态先于外部扩展就绪。
2. 内置工具、Workspace、Session、Provider 配置属于核心 Runtime；optional MCP 属于可降级扩展。
3. MCP 的 installed/configured/catalogued/connected 是不同状态。
4. MCP 启动状态应按 server 发布 Starting/Ready/Failed/Cancelled 等安全事实。
5. Skill 只在初始上下文暴露有界元数据，完整正文按需读取。
6. 每个 Run 使用稳定工具定义；运行途中不得改变 Schema。

不照搬 Codex 当前“无工具缓存时首轮等待 pending MCP `tools/list`”的已知缺陷。M20 对无缓存的
optional MCP 只做后台目录发现，不阻塞 CLI 或首个 Run。

## 3. 目标

### 3.1 必做

- CLI 在 Runtime 初始化期间立即展示可更新的安全阶段状态。
- `startup: optional` 不再同步启动 MCP 子进程，不阻塞 Runtime、Session 或输入框。
- `startup: required` 保持同步校验，失败时回滚 Runtime。
- MCP 安装/显式 probe 时保存脱敏、带配置指纹的完整工具目录。
- 有有效工具目录的 optional MCP 在 Runtime 中注册惰性工具；首次调用时才连接 server。
- 没有有效目录的 optional MCP 后台隔离发现，成功后写缓存，下一 Runtime 生效。
- MCP 目录和连接状态可动态查询；卸载最后一个配置 scope 时清理其受管目录缓存。
- Runtime 关闭时取消后台发现、关闭连接和子进程，保持幂等。
- Skill 元数据清单设置有界预算，正文仍只由 `load_skill` 按需读取。
- CLI `/mcp list` 展示 configured/catalogued/connecting/connected/degraded 等真实状态。
- 正式 Agent Service 文档记录新增生命周期语义和 API 接入影响。

### 3.2 可选

- 固定 Playwright MCP 示例版本，避免 `@latest` 每次检查；实际版本仅在验证可用版本后更新。
- 为启动阶段记录安全耗时指标，不向事件暴露路径、环境变量或原始异常。

### 3.3 不做

- 不修改 `agent/loop.py`。
- 不做全栈 async 改造。
- 不在运行中的 Run 动态增加或删除工具。
- 不在 Agent 仓库安装或内嵌业务 MCP server。
- 不修改 `assistant_agent_api` 或 `assistant_agent_web`。
- 不把 MCP 工具输出、密钥、环境变量或第三方原始异常写入目录缓存。

## 4. 设计

### 4.1 启动阶段

新增 UI 无关的 `RuntimeStartupEvent` 和可选 observer：

```text
loading_config
starting_workspace
discovering_skills
starting_web
preparing_mcp
creating_loop
ready
```

事件只包含阶段、状态和安全文本。CLI adapter 用 Rich 动态状态渲染；API 可忽略。observer 异常
不得破坏 Runtime 创建。

### 4.2 MCP 工具目录

目录记录位于 `ASSISTANT_AGENT_HOME` 下的受管 MCP 元数据目录，不进入项目源码或 workspace
会话日志。记录包含：

- schema version；
- server 名；
- MCP 配置的 SHA-256 指纹；
- 探测时间；
- 原始工具名、description、inputSchema、outputSchema、可信 annotations 白名单。

记录不包含 env 展开值、headers 展开值、密钥、工具调用参数或工具输出。写入采用临时文件加
`os.replace`；读取时校验版本、server、指纹和 JSON Schema。

### 4.3 optional MCP

```text
有效目录存在
  -> 立即从目录构造 MCPTool（不启动 server）
  -> Run 工具定义稳定
  -> 首次调用时连接、initialize、tools/list 并校验目标工具仍存在
  -> 执行 call_tool

无有效目录
  -> Runtime 不注册该 server 工具
  -> 后台隔离连接并发现目录
  -> 成功写缓存并标记 restart_required
  -> 当前 Runtime 不动态注入；下一 Runtime 生效
```

后台发现不得保留无用 MCP 子进程；完成目录写入后立即关闭探测连接。

### 4.4 required MCP

required server 保持 Runtime 创建期同步连接和工具发现。所有 required server 有界并行；任一失败
则关闭已连接 server 并抛 `RuntimeDependencyError`。成功工具立即注册并写目录。

### 4.5 工具稳定性

AgentLoop 继续在构造时冻结工具 Schema。M20 只在 Loop 创建前注册：

- 内置工具；
- Web 工具；
- extension 管理工具；
- required MCP 实时工具；
- optional MCP 的有效目录惰性工具。

后台新发现目录不修改 Registry，因此当前 Run、checkpoint 和恢复定义不受并发影响。

### 4.6 Skill

Skill 扫描保持同步文件元数据读取，不启动进程或网络。初始 prompt 中的 Skill catalog 按
`max_context_tokens` 的 2% token 预算约束，并设置 8,000 字符兜底上限；超出时稳定截断并产生
结构化 notice。完整 `SKILL.md` 仍通过 `load_skill` 按需读取。

### 4.7 状态模型

MCP 状态扩展为：

```text
disabled
blocked_by_policy
discovering
available_cached
restart_required
connecting
connected
degraded_timeout
degraded_connection
degraded_discovery
required_failed
```

Runtime 初始 capabilities 保持可用；`SessionRuntime.capabilities` 每次读取动态刷新 MCP 状态，不改变
StepEvent。`EVENT_CONTRACT_VERSION` 不提升。

## 5. 预计修改文件

- `contracts/capabilities.py`：启动事件与 MCP 状态 additive 扩展。
- `contracts/__init__.py`、`service/__init__.py`：公共 DTO re-export。
- `config/schema.py`、`config/paths.py`：Skill catalog 预算和 MCP catalog 路径。
- `integrations/mcp/catalog.py`：新工具目录所有者。
- `integrations/mcp/discovery.py`：发现结果与工具构造解耦。
- `integrations/mcp/manager.py`：required 同步、optional 惰性连接和后台发现。
- `integrations/mcp/configure.py`：probe/add 写目录，remove 清理。
- `application/ports.py`、`application/runtime.py`：动态能力读取端口。
- `bootstrap/runtime.py`、`bootstrap/tools.py`：唯一装配和启动 observer。
- `cli/setup.py`、`cli/extensions.py`、`cli/commands.py`、`ui/console.py`：启动状态和 MCP 状态展示。
- `config.example.yaml`、README、ARCHITECTURE、ROADMAP、TECH_DEBT、正式服务契约。
- 对应 tests/contract/eval 测试。

## 6. 测试计划

1. optional MCP 有缓存时 Runtime 不启动进程且工具立即注册。
2. optional MCP 第一次工具调用才连接，后续复用同一连接。
3. optional MCP 无缓存时 Runtime 快速返回，后台发现写目录并关闭探测连接。
4. 后台 timeout/failure 不影响 Runtime，状态安全降级。
5. required MCP 继续阻塞校验并在失败时完整回滚。
6. 配置指纹变化使旧目录失效。
7. 损坏、超限或含非法 Schema 的目录安全忽略。
8. 目录不保存 env/header 展开值、工具输出或调用参数。
9. close 在后台发现、惰性连接和已连接三种状态下均幂等且无遗留线程/进程。
10. 启动 observer 顺序稳定，observer 异常不影响 Runtime。
11. Skill metadata 超预算时稳定截断并给 notice，完整正文仍可按需加载。
12. CLI `/mcp list` 展示动态状态。
13. Session/Run、恢复定义、final/run_terminal 顺序和现有测试不回退。

## 7. 验收标准

- 配置两个 optional 本地 MCP 时，CLI 首屏和输入框不等待其进程启动。
- 无 MCP 连接时内置工具、Skill、Session 和模型对话可正常使用。
- 有有效目录的 MCP 首次实际调用才启动服务，调用链与权限/审计/恢复语义不变。
- required MCP 行为保持 M17 契约。
- `pytest`、coverage、Ruff、mypy、import-linter 全绿；scripted/recovery eval 不回退。
- 无密钥、缓存、日志和 MCP 业务源码进入 Git。
- 正式服务契约和 API AI 交接同步完成。

## 8. 风险与回退

- **目录陈旧**：调用前重新连接并确认原始工具仍存在；缺失时返回稳定依赖错误，不调用错误工具。
- **首次手写配置无目录**：当前 Runtime 不暴露未知 Schema，后台发现后明确提示下一 Runtime 生效。
- **供应链漂移**：配置指纹无法识别 `@latest` 背后版本变化；文档要求固定版本，后台连接仍以实时
  `tools/list` 校验目标工具。
- **并发关闭**：后台任务统一由 MCPManager 拥有并在 close 时取消、等待和关闭 transport。
- **API 兼容**：仅新增 DTO/状态和动态能力语义；旧调用方忽略未知状态时需按 unavailable 处理。

## 9. 实施与验收结果

M20 已按本方案完成，未修改 `agent/loop.py`。实测结果：

- 首次无目录 Runtime：约 2.48 秒；optional MCP 后台发现，不阻塞输入框；
- 后台约 5 秒完成后状态为 `restart_required`，探测连接已关闭；
- 第二次有目录 Runtime：约 1.28 秒，工具状态为 `available_cached`，未启动 MCP 进程；
- 原完整 Runtime 基线约 9.98 秒；
- 618 passed、5 skipped，覆盖率 83%；Ruff、mypy、12/12 import-linter 全绿；
- scripted 18/18（27 tool calls，tokens 120/31），recovery 4/4；
- 生产 Python 14,360 行/123 文件，eval 基础设施 1,404 行。

实施中额外修复了三项边界：失败启动阶段发布 `failed` 而非误报 `completed`；惰性连接后 capability
仍只展示当前 Runtime 实际注册的过滤后工具；MCP 工具目录指纹不派生 env/header 凭据值。关闭后台
发现时显式让 event loop 消费取消，`RuntimeWarning` 提升为 error 的回归测试通过。

用户验收时发现模型会搜索项目目录猜测 MCP 配置。现已新增只读 `inspect_runtime`，直接查询当前
Registry、可见 Skill 和 MCPManager 动态状态；`configure_mcp_server list` 仅用于配置清单。核心工具
优先占用 Schema 预算，optional MCP 仅使用剩余空间，避免外部 Schema 挤垮 Runtime。
该修复只增加 Agent 内部工具及 notice code，未改变 StepEvent、RuntimeCapabilities 字段、Interaction、
Run/Session 生命周期或 checkpoint；公共服务契约与 API 必改项无新增变化。

`integrations/mcp/manager.py` 物理行数 720，已按 600 行预警规则在 `docs/ARCHITECTURE.md` 完成内聚性
评审，当前不机械拆分。调用期连续失败熔断仍属于 D21，不在 M20 范围内。
