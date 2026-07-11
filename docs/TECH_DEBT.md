# 技术债登记册

> AI 迭代开发中，债务会隐形复利（LLM 在每个决策点埋入未言明的假设）。
> 这里显式追踪，防止"上次说的债"下次忘。每次里程碑评审更新本表。
> 最后更新：2026-07-08（M6 后：obs 层落地；D7 触线已化解；新增 D8 观测缺口。剩 D5/D6/D7/D8）

## 状态说明
- 🔴 高：影响正确性/安全，或脆弱的关键路径
- 🟡 中：影响可维护性，暂不影响功能
- 🟢 低：整洁度问题

## 登记表

| # | 债务 | 位置 | 级别 | 风险 | 计划 |
|---|------|------|:---:|------|------|
| D5 | **UI 层测试仍薄** | `ui/console.py`、`main.py` | 🟡 | render_stream 状态机、SIGINT 仍仅手验（session/input 部分已间接覆盖） | 不必全补；触及时补 |
| D6 | **provider 与 model 未分层** | `config/schema.py` providers | 🟢 | 一条目=一模型；同厂商多模型要重复写 api_base/api_key（key 可用 ${VAR} 缓解） | **暂不做（YAGNI）**。触发信号：同一厂商挂 **4+ 个模型**、重复条目变烦时，重构成"backends（连接层）+ models（复用 backend）"两层（参考 Codex model_providers）。当前 2-3 模型不值得改 schema |
| D7 | **两文件逼近 300 行上限** | `tools/file_ops.py`(278)、`main.py`(298) | 🟢 | 架构测试上限 300，余量已很小；再加能力（新文件工具 / 新 CLI 子命令）会撞线 | M6 期间 main.py 曾触线（305 报红），以"logger 构建外移到 `obs.create_logger` 工厂"化解回 298——但余量更小了。下次再触线应真正拆分：file_ops 按"读/写/编辑"分组、main 抽出 CLI 子命令模块 |
| D8 | **M6 日志的观测缺口（4 项）** | `obs/logger.py`、`tools/registry.py`、`main.py` | 🟡 | M6 复盘发现，均非阻断但影响日志的准确性/完整性：①`run_shell` 的 `duration_ms` 含"等用户确认"时间（确认在 `tool.run` 内部，计时包住整个 run），语义像"命令跑了 8.8s"实则等人；②脱敏不递归嵌套结构（`multi_edit.edits[].new_string` 里的密钥不遮蔽），当前触发概率低；③chat 里 `/clear` 换会话后日志 `session_id` 不变（日志按"进程运行"粒度、与 SessionStore 会话 id 不同名却像）；④`/model` 切模型后 `session_start` 的 model 已过时，tool_call 不带模型信息，无法从日志看出某调用用的哪个模型 | 触发信号：①做审计/性能分析、要用 duration 判断命令快慢时——把确认移到工具外或计时只包实际执行；②日志里真出现嵌套密钥泄露时——`_redact` 递归 dict/list；③④要按会话/模型维度聚合日志时——`/clear` 补一条 `session_start`、`/model` 记 `model_switch` 事件。现都不做 |

## 已还清（保留记录）
- **D1 流式碎片拼接无测试** → M3 补 `tests/test_client.py`：碎片拼接、多工具、坏 JSON 兜底、usage、代理豁免均有直接单测。✅ 2026-07-02
- **D4 main 戳 Console 私有属性** → 加 `Console.input()` 收口，main 改用之。✅ 2026-07-02
- **D2 非流式死代码** → 删除 `complete`/`_normalize`/`LLMResponse`/`wants_tools`（生产与测试均不调用；ToolCall/_normalize_usage 保留）。✅ 2026-07-02
- **D3 EventKind 陈旧成员** → 从 Literal 移除 `"assistant"`（循环已不发、console 不处理）。✅ 2026-07-02

## 备注
- `context.py` 的"按消息数截断"不列为债——它是 M3 的**正式工作项**（token 感知截断），见 [m3-memory-plan.md](archive/phase1/m3-memory-plan.md)。
