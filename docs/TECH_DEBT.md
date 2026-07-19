# 技术债登记册

> AI 迭代开发中，债务会隐形复利（LLM 在每个决策点埋入未言明的假设）。
> 这里显式追踪，防止"上次说的债"下次忘。每次里程碑评审更新本表。
> 最后更新：2026-07-19（M21 受管命令生命周期：637 passed/6 skipped、覆盖率 84%，
> Ruff/format/mypy、12 条 import-linter、scripted 18/18、recovery 4/4 全绿；生产源码
> 14,897 行/125 文件。前台命令完整 deadline 和 Runtime 隔离后台进程还清 D23。）

## 状态说明
- 🔴 高：影响正确性/安全，或脆弱的关键路径
- 🟡 中：影响可维护性，暂不影响功能
- 🟢 低：整洁度问题

## 登记表

| # | 债务 | 位置 | 级别 | 风险 | 计划 |
|---|------|------|:---:|------|------|
| D5 | **UI 交互层测试仍薄** | `ui/console.py`、`main.py` | 🟡 | M15 补齐 Activity 动态计时/单 Live 生命周期、Markdown 空闲反馈、renderer 阶段与异常清理、授权/继续恢复和 quiet 回归；真实 SIGINT、TTY 自动刷新与真实键盘行编辑仍主要靠手验 | 不必全补；触及时补 |
| D6 | **provider 与 model 未分层** | `config/schema.py` providers | 🟢 | 一条目=一模型；同厂商多模型要重复写 api_base/api_key（key 可用 ${VAR} 缓解） | **暂不做（YAGNI）**。触发信号：同一厂商挂 **4+ 个模型**、重复条目变烦时，重构成"backends（连接层）+ models（复用 backend）"两层（参考 Codex model_providers）。当前 2-3 模型不值得改 schema |
| D7 | ~~**main.py 越过行数软线**~~ ✅ 已还清（M7b）| `main.py`(222)、`cli/setup.py` | ✅ | M7b 把 wiring（build_runtime + Runtime 上下文管理器）抽到 `cli/setup.py`，main.py 329→222，回落软线 300 以下。`_interrupt` 保留在 main（信号处理是 CLI 关注点），以 `interrupt_check` 参数注入。**剩 file_ops.py(278)** 仍近软线，触及"读/写/编辑"再分组 |
| D8 | ~~**日志按会话/模型维度的缺口**~~ ✅ 已还清（M10b） | `obs/logger.py`、`cli/recovery.py` | ✅ | `trace_id`（进程）、`session_id`（聊天）、`run_id`（任务）拆分；`/clear` 重新绑定 Session；`model_switch` 更新后续事件模型；tool_call 带 run/call/provider/model；恢复与 checkpoint 有独立事件 | 新旧 JSONL 字段兼容；日志仍是尽力而为的观测，不参与 checkpoint 正确性 |
| D9 | ~~**无行为级 eval 任务集**~~ ✅ 已还清（M9c） | `evals/`、`tests/test_evals.py` | ✅ | 版本化 YAML case + fixture confinement + scripted/real runner + 可解释 scorer + JSONL/Markdown 报告 + A/B compare；14 个 deterministic case 进入 CI，真实 provider 轨道不进 PR 硬门 | 303 测试基线覆盖 loader/scorer/runner/report/CLI、权限/预算/终止和 Runtime/UI 补测；方案见 [归档计划](archive/phase3/m9c-agent-evals-plan.md) |
| D10 | ~~**上下文预算未计入工具 schema**~~ ✅ 已还清（M8a）| `agent/context.py`、`agent/loop.py` | ✅ | M8a 统一预算口径：可用消息预算 = 窗口 − system − tools schema − reserved_output。tools schema 由 loop（持 registry）估算注入 context（context 保持被动、不反依赖 registry）；reserved_output 默认 1024 保证回复空间；`/context` 分项显示真实占用。实测内置工具 schema 3208 token 现已计入（原完全不计）。**两开销默认归零时预算与旧行为逐字节一致（回归保护）** |
| D11 | ~~**十一个文件越过旧行数软线**~~ ✅ 已还清（M19） | `docs/ARCHITECTURE.md`、`.importlinter` | ✅ | 旧 300/500 机械门禁被概念所有权、依赖契约、C901 循环检查和 600 行非阻断评审替代；Runtime/Session/Run、Provider/Tool 与 adapter 职责已归位，当前无超过 600 行生产模块 | 行数不再单独形成债；首次超过 600 行时按职责、状态不变量、依赖与测试定位做具体评审并留档 |
| D12 | **摘要压缩为整段、无选择性/检索** | `agent/context/compaction.py` | 🟢 | M8b 先做整段摘要（最旧轮压成要点）。工具结果选择性压缩、语义检索式记忆（RAG/向量）、跨会话记忆均未做 | 信号驱动的未来方向：长会话里"早期某具体事实被摘要糊掉、后续又要精确引用"反复出现时，再考虑选择性保留或检索式记忆。当前整段摘要够用 |
| D13 | ~~**上下文预算不是最终硬保证**~~ ✅ 已还清（M9a） | `agent/token_budget.py`、`agent/context.py` | ✅ | 最终封套出口强制 `used <= window`；超大用户输入在 provider 调用前稳定拒绝，摘要受硬上限并为最新任务让位；估算器可替换且失败回退保守口径 | 258 测试基线覆盖超大消息、摘要、坏 checkpoint 与不调用 client |
| D14 | ~~**权限边界可被 Shell/区外读取绕过**~~ ✅ 已还清（M9b） | `tools/permissions.py`、`tools/policy.py`、`tools/registry.py` | ✅ | Registry 在预算与 Tool.run 前强制统一门控；文件/进程/网络/MCP/Skill capability 独立决策；未知 Tool 默认 ask；Shell 仅证明极小只读集合，其余保守声明广泛能力；区外读写和敏感目录受控；提示词/banner 明确无 OS 沙箱 | 281 测试覆盖优先级、非交互拒绝、精确会话授权、observer fail-closed、Shell/Git 绕过、Skill/MCP 信任与脱敏 |
| D15 | ~~**会话存储与 Runtime 失败清理不够安全**~~ ✅ 已还清（M9a） | `session/store.py`、`mcp/manager.py`、`cli/setup.py` | ✅ | Session ID 校验 + confinement；同目录临时文件、fsync、`os.replace` 原子保存；MCP initialize 与 Runtime 构造失败逆序清理 | 故障注入测试验证旧文件保留、partial stack/MCP/logger 关闭 |
| D16 | ~~**工具 I/O 不适合大文件与无界输出**~~ ✅ 已还清（M10a） | `tools/`、`mcp/tool.py` | ✅ | 运行时 JSON Schema 校验；范围读取与流式搜索；Shell/Git 双流有界捕获和受限 Artifact；write/edit/multi_edit 同目录原子替换并保持换行/权限；MCP structuredContent 保真；`file_ops.py` 拆为兼容 facade | 335 测试与 18 个 deterministic eval 覆盖 10 万行中段读取、坏参数恢复、双 PIPE 压力、Artifact、原子写故障和结构化 MCP 结果；方案见 [归档计划](archive/phase3/m10a-tool-contract-plan.md) |
| D17 | ~~**模型切换后的运行状态不一致**~~ ✅ 已还清（M9a） | `agent/loop.py`、`cli/commands.py`、`session/store.py` | ✅ | 默认 Compactor 跟随新 client，固定摘要 provider 不变；Session 元数据与 `model_switch` 审计同步；resume 明确沿用当前配置 | M9a 回归测试覆盖 |
| D18 | ~~**Shell 超时未终止完整子进程树**~~ ✅ 已还清（M14a/M21） | `execution/process.py`、`execution/process_windows.py` | ✅ | M14a 建立 Windows Job Object/POSIX process group；M21 补齐父进程先退出、后代继承 PIPE 时的完整 deadline、有界 drain/cleanup 和 Windows `start /b`/POSIX 后台回归 | 不以全栈 async 重构解决；第三方同步 LLM/Web 仍由自身 timeout 和安全边界控制，不能宣称毫秒级抢占 |
| D19 | ~~**CLI 展示透传执行载荷，缺少语义摘要和详细度分层**~~ ✅ 已还清（M11a） | `tools/display.py`、`ui/`、`agent/events.py` | ✅ | ToolDisplay 语义摘要；normal/verbose/quiet；quiet 仅隐藏 Agent 轨迹、不隐藏 slash 控制面；normal 临时活动区不沉淀工具间旁白；Write/Edit 权限前有界预览与整块背景；全宽输入边界与会话启停 ID；参数、metadata、模型文本统一终端脱敏；15 FPS 流式 Markdown；错误自动展开 | 423 测试覆盖模式矩阵、quiet slash、写入/diff 背景/截断/脱敏、确认顺序、会话生命周期、过程丢弃/最终单次提交、碎片 Markdown、ANSI/密钥、工具 metadata；方案见 [归档计划](archive/phase4/m11a-cli-conversation-ui-plan.md) |
| D20 | **MCP stdio stderr 单次长运行缺少在线硬上限** | `integrations/mcp/manager.py` | 🟢 | stderr 已与统一审计和 artifact 分离并落入 workspace 诊断目录，但 SDK 直接把子进程 fd 指向文件；M14 的 ProcessSupervisor 不拥有 SDK 创建的 stdio 子进程，异常 server 长时间刷 stderr 仍可能增长 | 独立评估 MCP SDK 自定义 transport/pipe drain 与在线轮转；当前文档不宣称 stderr 已硬限流 |
| D21 | **MCP 调用期缺少连续失败熔断** | `integrations/mcp/manager.py`、`cli/extensions.py` | 🟡 | M20 已补 configured/catalogued/connecting/connected/degraded 动态状态、optional 按需连接和后台目录发现；但 server 在 Run 中死亡后，后续调用仍会逐次触发 transport error，`/mcp doctor` 尚不展示连续失败计数或 breaker 状态 | 出现长会话 server 崩溃/重复失败时立项：实现调用期连续失败熔断和 health 展示；发送后绝不自动重放，未知副作用仍走 recovery |
| D22 | ~~**Loop/Recovery 接近单文件硬线**~~ ✅ 已还清（M19） | `agent/loop.py`(436)、`agent/run/` | ✅ | 单轮模型流、工具批次、预算、恢复、checkpoint 和 resume 已按状态所有权拆分；Loop 保留可见且只编排 Agent 算法。scripted 事件轨迹与 recovery fault injection 前后不变 | 后续按独立变化原因拆分，不恢复行数硬线，也不为降低数字拆散状态不变量 |
| D23 | ~~**父进程先退出时进程输出收尾可无限等待**~~ ✅ 已还清（M21） | `execution/process.py`、`execution/jobs.py` | ✅ | 完整 deadline 覆盖 execution/drain/cleanup；遗留 PIPE 后代结构化失败并清理；`manage_process` 提供 Runtime 隔离的启动/状态/日志/停止，opaque ID、输出和历史均有界 | Windows `start /b` 与通用继承 PIPE 实测；POSIX shell 后台路径进入 CI。方案见 [归档](archive/phase14/m21-managed-command-lifecycle-plan.md) |

## 已还清（保留记录）
- **任务级工具资源无边界** → M6.5 增加单次输出、累计输出、工具调用总数预算；多 tool-call 批次补齐结果后终止。✅ 2026-07-14
- **一期预算配置/计时语义偏差** → 单次限制迁移到 ToolsConfig；确认等待累计；日志补齐 wall/execution/returned length。✅ 2026-07-14
- **D8① 确认等待混入 duration** → `request_confirm` 测确认回调墙钟耗时，`registry.execute` 从总耗时剥离，单列 `approval_wait_ms`。✅ 2026-07-13
- **D8② 脱敏不递归嵌套** → `_sanitize_value` 递归 dict/list/str，覆盖 `multi_edit.edits[].new_string` 等嵌套密钥。✅ 2026-07-13
- **D1 流式碎片拼接无测试** → M3 补 `tests/test_client.py`：碎片拼接、多工具、坏 JSON 兜底、usage、代理豁免均有直接单测。✅ 2026-07-02
- **D4 main 戳 Console 私有属性** → 加 `Console.input()` 收口，main 改用之。✅ 2026-07-02
- **D2 非流式死代码** → 删除 `complete`/`_normalize`/`LLMResponse`/`wants_tools`（生产与测试均不调用；ToolCall/_normalize_usage 保留）。✅ 2026-07-02
- **D3 EventKind 陈旧成员** → 从 Literal 移除 `"assistant"`（循环已不发、console 不处理）。✅ 2026-07-02

## 备注
- `context.py` 的"按消息数截断"不列为债——它是 M3 的**正式工作项**（token 感知截断），见 [m3-memory-plan.md](archive/phase1/m3-memory-plan.md)。
