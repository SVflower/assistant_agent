# 技术债登记册

> AI 迭代开发中，债务会隐形复利（LLM 在每个决策点埋入未言明的假设）。
> 这里显式追踪，防止"上次说的债"下次忘。每次里程碑评审更新本表。
> 最后更新：2026-07-14（M6.5 运行时预算完成；新增 D10 上下文预算口径。剩 D5/D6/D7/D8/D9/D10）

## 状态说明
- 🔴 高：影响正确性/安全，或脆弱的关键路径
- 🟡 中：影响可维护性，暂不影响功能
- 🟢 低：整洁度问题

## 登记表

| # | 债务 | 位置 | 级别 | 风险 | 计划 |
|---|------|------|:---:|------|------|
| D5 | **UI 层测试仍薄** | `ui/console.py`、`main.py` | 🟡 | render_stream 状态机、SIGINT 仍仅手验（session/input 部分已间接覆盖） | 不必全补；触及时补 |
| D6 | **provider 与 model 未分层** | `config/schema.py` providers | 🟢 | 一条目=一模型；同厂商多模型要重复写 api_base/api_key（key 可用 ${VAR} 缓解） | **暂不做（YAGNI）**。触发信号：同一厂商挂 **4+ 个模型**、重复条目变烦时，重构成"backends（连接层）+ models（复用 backend）"两层（参考 Codex model_providers）。当前 2-3 模型不值得改 schema |
| D7 | **两文件在行数软线附近** | `tools/file_ops.py`(278)、`main.py`(298) | 🟢 | 质量护栏方案已把行数改分级：软线 300 仅警告、硬线 500 才失败。这两文件在软线下、离硬线远，风险已大幅下降——不再"随时触雷"，只是评审信号 | 不必为凑行数硬拆。真正膨胀（逼近 500）时再拆：file_ops 按"读/写/编辑"分组、main 抽 CLI 子命令模块 |
| D8 | **日志按会话/模型维度的缺口（原 4 项剩 2）** | `main.py`、`obs/logger.py` | 🟡 | ①② 已还清（见下）；剩：③chat 里 `/clear` 换会话后日志 `session_id` 不变（日志按"进程运行"粒度、与 SessionStore 会话 id 不同名却像）；④`/model` 切模型后 `session_start` 的 model 已过时，tool_call 不带模型信息，无法从日志看出某调用用的哪个模型 | 触发信号：要按会话/模型维度聚合日志时——`/clear` 补一条 `session_start`、`/model` 记 `model_switch` 事件。现不做 |
| D9 | **无行为级 eval 任务集** | （待建 `evals/`） | 🟡 | 单元测试验证组件，但无法持续比较"模型 A/B 是否完成任务、是否错误调用工具、是否越预算"。质量护栏方案评审时确认：光有 YAML 案例无 runner＝纸面契约，故推迟 | 触发信号：需对比模型 A/B 完成度、或回归"越权调用/越预算"行为时——建 YAML 案例（id/task/tools/expect/budget）+ fake-client runner，不接真实模型 CI。方案见 [归档计划](archive/phase2/quality-guardrails-final-plan.md) |
| D10 | **上下文预算未计入工具 schema** | `agent/context.py`、`agent/loop.py` | 🟡 | 当前 token 近似只统计 system + messages；默认工具 schema 约 3454 字符未计入。M6.5 已用单次/累计输出预算降低风险，但窗口很小的本地模型仍可能比配置预算更早溢出 | M8 上下文进化时统一处理：模型感知 token 估算需计入工具 schema、system、消息和预留输出，不在 M6.5 临时重复一套估算 |

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
