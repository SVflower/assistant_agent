# 技术债登记册

> AI 迭代开发中，债务会隐形复利（LLM 在每个决策点埋入未言明的假设）。
> 这里显式追踪，防止"上次说的债"下次忘。每次里程碑评审更新本表。
> 最后更新：2026-07-15（M11a CLI 展示重构完成，还清 D19；方案见
> [M11a 归档](archive/phase4/m11a-cli-conversation-ui-plan.md)。）

## 状态说明
- 🔴 高：影响正确性/安全，或脆弱的关键路径
- 🟡 中：影响可维护性，暂不影响功能
- 🟢 低：整洁度问题

## 登记表

| # | 债务 | 位置 | 级别 | 风险 | 计划 |
|---|------|------|:---:|------|------|
| D5 | **UI 交互层测试仍薄** | `ui/console.py`、`main.py` | 🟡 | M11a 已覆盖 renderer/Markdown/展示模式、“写入预览先于确认”顺序，以及 chat 新建→`/clear`→退出的真实会话 ID；真实 SIGINT、TTY Live 与真实键盘行编辑仍主要靠手验 | 不必全补；触及时补 |
| D6 | **provider 与 model 未分层** | `config/schema.py` providers | 🟢 | 一条目=一模型；同厂商多模型要重复写 api_base/api_key（key 可用 ${VAR} 缓解） | **暂不做（YAGNI）**。触发信号：同一厂商挂 **4+ 个模型**、重复条目变烦时，重构成"backends（连接层）+ models（复用 backend）"两层（参考 Codex model_providers）。当前 2-3 模型不值得改 schema |
| D7 | ~~**main.py 越过行数软线**~~ ✅ 已还清（M7b）| `main.py`(222)、`cli/setup.py` | ✅ | M7b 把 wiring（build_runtime + Runtime 上下文管理器）抽到 `cli/setup.py`，main.py 329→222，回落软线 300 以下。`_interrupt` 保留在 main（信号处理是 CLI 关注点），以 `interrupt_check` 参数注入。**剩 file_ops.py(278)** 仍近软线，触及"读/写/编辑"再分组 |
| D8 | ~~**日志按会话/模型维度的缺口**~~ ✅ 已还清（M10b） | `obs/logger.py`、`cli/recovery.py` | ✅ | `trace_id`（进程）、`session_id`（聊天）、`run_id`（任务）拆分；`/clear` 重新绑定 Session；`model_switch` 更新后续事件模型；tool_call 带 run/call/provider/model；恢复与 checkpoint 有独立事件 | 新旧 JSONL 字段兼容；日志仍是尽力而为的观测，不参与 checkpoint 正确性 |
| D9 | ~~**无行为级 eval 任务集**~~ ✅ 已还清（M9c） | `evals/`、`tests/test_evals.py` | ✅ | 版本化 YAML case + fixture confinement + scripted/real runner + 可解释 scorer + JSONL/Markdown 报告 + A/B compare；14 个 deterministic case 进入 CI，真实 provider 轨道不进 PR 硬门 | 303 测试基线覆盖 loader/scorer/runner/report/CLI、权限/预算/终止和 Runtime/UI 补测；方案见 [归档计划](archive/phase3/m9c-agent-evals-plan.md) |
| D10 | ~~**上下文预算未计入工具 schema**~~ ✅ 已还清（M8a）| `agent/context.py`、`agent/loop.py` | ✅ | M8a 统一预算口径：可用消息预算 = 窗口 − system − tools schema − reserved_output。tools schema 由 loop（持 registry）估算注入 context（context 保持被动、不反依赖 registry）；reserved_output 默认 1024 保证回复空间；`/context` 分项显示真实占用。实测内置工具 schema 3208 token 现已计入（原完全不计）。**两开销默认归零时预算与旧行为逐字节一致（回归保护）** |
| D11 | **六个文件越过行数软线** | `agent/context.py`(342)、`agent/loop.py`(419)、`agent/recovery.py`(452)、`main.py`(318)、`obs/logger.py`(349)、`tools/registry.py`(378) | 🟢 | 全部低于硬线 500；M11a 把 `ui/console.py` 从 303 行拆到 207 行并退出软线；剩余文件是内聚状态机或协议编排，不为 300 软线机械切碎 | 任一文件继续增加独立职责或逼近硬线时再拆；`recovery.py` 优先按“状态转换/定义兼容”边界评审，Loop 禁止放宽硬线 |
| D12 | **摘要压缩为整段、无选择性/检索** | `agent/compaction.py` | 🟢 | M8b 先做整段摘要（最旧轮压成要点）。工具结果选择性压缩、语义检索式记忆（RAG/向量）、跨会话记忆均未做 | 信号驱动的未来方向：长会话里"早期某具体事实被摘要糊掉、后续又要精确引用"反复出现时，再考虑选择性保留或检索式记忆。当前整段摘要够用 |
| D13 | ~~**上下文预算不是最终硬保证**~~ ✅ 已还清（M9a） | `agent/token_budget.py`、`agent/context.py` | ✅ | 最终封套出口强制 `used <= window`；超大用户输入在 provider 调用前稳定拒绝，摘要受硬上限并为最新任务让位；估算器可替换且失败回退保守口径 | 258 测试基线覆盖超大消息、摘要、坏 checkpoint 与不调用 client |
| D14 | ~~**权限边界可被 Shell/区外读取绕过**~~ ✅ 已还清（M9b） | `tools/permissions.py`、`tools/policy.py`、`tools/registry.py` | ✅ | Registry 在预算与 Tool.run 前强制统一门控；文件/进程/网络/MCP/Skill capability 独立决策；未知 Tool 默认 ask；Shell 仅证明极小只读集合，其余保守声明广泛能力；区外读写和敏感目录受控；提示词/banner 明确无 OS 沙箱 | 281 测试覆盖优先级、非交互拒绝、精确会话授权、observer fail-closed、Shell/Git 绕过、Skill/MCP 信任与脱敏 |
| D15 | ~~**会话存储与 Runtime 失败清理不够安全**~~ ✅ 已还清（M9a） | `session/store.py`、`mcp/manager.py`、`cli/setup.py` | ✅ | Session ID 校验 + confinement；同目录临时文件、fsync、`os.replace` 原子保存；MCP initialize 与 Runtime 构造失败逆序清理 | 故障注入测试验证旧文件保留、partial stack/MCP/logger 关闭 |
| D16 | ~~**工具 I/O 不适合大文件与无界输出**~~ ✅ 已还清（M10a） | `tools/`、`mcp/tool.py` | ✅ | 运行时 JSON Schema 校验；范围读取与流式搜索；Shell/Git 双流有界捕获和受限 Artifact；write/edit/multi_edit 同目录原子替换并保持换行/权限；MCP structuredContent 保真；`file_ops.py` 拆为兼容 facade | 335 测试与 18 个 deterministic eval 覆盖 10 万行中段读取、坏参数恢复、双 PIPE 压力、Artifact、原子写故障和结构化 MCP 结果；方案见 [归档计划](archive/phase3/m10a-tool-contract-plan.md) |
| D17 | ~~**模型切换后的运行状态不一致**~~ ✅ 已还清（M9a） | `agent/loop.py`、`cli/commands.py`、`session/store.py` | ✅ | 默认 Compactor 跟随新 client，固定摘要 provider 不变；Session 元数据与 `model_switch` 审计同步；resume 明确沿用当前配置 | M9a 回归测试覆盖 |
| D18 | **Shell 超时未终止完整子进程树** | `tools/process.py` | 🟡 | timeout 会 kill/wait 直接 `Popen` 进程，但 `shell=True` 命令派生的子进程可能继续存活；当前不应宣称具备进程树级取消 | M10c 决定不以全栈 async 重构解决；后续独立立项 ProcessSupervisor，Windows Job Object / POSIX process group 经跨平台故障测试后才可还清 |
| D19 | ~~**CLI 展示透传执行载荷，缺少语义摘要和详细度分层**~~ ✅ 已还清（M11a） | `tools/display.py`、`ui/`、`agent/events.py` | ✅ | ToolDisplay 语义摘要；normal/verbose/quiet；normal 临时活动区不沉淀工具间旁白；Write/Edit 权限前有界预览；全宽输入边界与会话启停 ID；参数、metadata、模型文本统一终端脱敏；15 FPS 流式 Markdown；错误自动展开 | 420 测试覆盖模式矩阵、写入/diff 截断与脱敏、确认顺序、会话生命周期、过程丢弃/最终单次提交、碎片 Markdown、ANSI/密钥、工具 metadata；方案见 [归档计划](archive/phase4/m11a-cli-conversation-ui-plan.md) |

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
