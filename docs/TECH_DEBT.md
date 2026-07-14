# 技术债登记册

> AI 迭代开发中，债务会隐形复利（LLM 在每个决策点埋入未言明的假设）。
> 这里显式追踪，防止"上次说的债"下次忘。每次里程碑评审更新本表。
> 最后更新：2026-07-14（M8a 预算口径完成；D10 已还清——tools schema + reserved 计入预算。剩 D5/D6/D8/D9）

## 状态说明
- 🔴 高：影响正确性/安全，或脆弱的关键路径
- 🟡 中：影响可维护性，暂不影响功能
- 🟢 低：整洁度问题

## 登记表

| # | 债务 | 位置 | 级别 | 风险 | 计划 |
|---|------|------|:---:|------|------|
| D5 | **UI 层测试仍薄** | `ui/console.py`、`main.py` | 🟡 | render_stream 状态机、SIGINT 仍仅手验（session/input 部分已间接覆盖） | 不必全补；触及时补 |
| D6 | **provider 与 model 未分层** | `config/schema.py` providers | 🟢 | 一条目=一模型；同厂商多模型要重复写 api_base/api_key（key 可用 ${VAR} 缓解） | **暂不做（YAGNI）**。触发信号：同一厂商挂 **4+ 个模型**、重复条目变烦时，重构成"backends（连接层）+ models（复用 backend）"两层（参考 Codex model_providers）。当前 2-3 模型不值得改 schema |
| D7 | ~~**main.py 越过行数软线**~~ ✅ 已还清（M7b）| `main.py`(222)、`cli/setup.py` | ✅ | M7b 把 wiring（build_runtime + Runtime 上下文管理器）抽到 `cli/setup.py`，main.py 329→222，回落软线 300 以下。`_interrupt` 保留在 main（信号处理是 CLI 关注点），以 `interrupt_check` 参数注入。**剩 file_ops.py(278)** 仍近软线，触及"读/写/编辑"再分组 |
| D8 | **日志按会话/模型维度的缺口（原 4 项剩 2）** | `main.py`、`obs/logger.py` | 🟡 | ①② 已还清（见下）；剩：③chat 里 `/clear` 换会话后日志 `session_id` 不变（日志按"进程运行"粒度、与 SessionStore 会话 id 不同名却像）；④`/model` 切模型后 `session_start` 的 model 已过时，tool_call 不带模型信息，无法从日志看出某调用用的哪个模型 | 触发信号：要按会话/模型维度聚合日志时——`/clear` 补一条 `session_start`、`/model` 记 `model_switch` 事件。现不做 |
| D9 | **无行为级 eval 任务集** | （待建 `evals/`） | 🟡 | 单元测试验证组件，但无法持续比较"模型 A/B 是否完成任务、是否错误调用工具、是否越预算"。质量护栏方案评审时确认：光有 YAML 案例无 runner＝纸面契约，故推迟 | 触发信号：需对比模型 A/B 完成度、或回归"越权调用/越预算"行为时——建 YAML 案例（id/task/tools/expect/budget）+ fake-client runner，不接真实模型 CI。方案见 [归档计划](archive/phase2/quality-guardrails-final-plan.md) |
| D10 | ~~**上下文预算未计入工具 schema**~~ ✅ 已还清（M8a）| `agent/context.py`、`agent/loop.py` | ✅ | M8a 统一预算口径：可用消息预算 = 窗口 − system − tools schema − reserved_output。tools schema 由 loop（持 registry）估算注入 context（context 保持被动、不反依赖 registry）；reserved_output 默认 1024 保证回复空间；`/context` 分项显示真实占用。实测内置工具 schema 3208 token 现已计入（原完全不计）。**两开销默认归零时预算与旧行为逐字节一致（回归保护）** |

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
