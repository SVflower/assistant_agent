# 技术债登记册

> AI 迭代开发中，债务会隐形复利（LLM 在每个决策点埋入未言明的假设）。
> 这里显式追踪，防止"上次说的债"下次忘。每次里程碑评审更新本表。
> 最后更新：2026-07-02（M4.7 后：D2/D3 已还，剩 D5/D6）

## 状态说明
- 🔴 高：影响正确性/安全，或脆弱的关键路径
- 🟡 中：影响可维护性，暂不影响功能
- 🟢 低：整洁度问题

## 登记表

| # | 债务 | 位置 | 级别 | 风险 | 计划 |
|---|------|------|:---:|------|------|
| D5 | **UI 层测试仍薄** | `ui/console.py`、`main.py` | 🟡 | render_stream 状态机、SIGINT 仍仅手验（session/input 部分已间接覆盖） | 不必全补；触及时补 |
| D6 | **provider 与 model 未分层** | `config/schema.py` providers | 🟢 | 一条目=一模型；同厂商多模型要重复写 api_base/api_key（key 可用 ${VAR} 缓解） | **暂不做（YAGNI）**。触发信号：同一厂商挂 **4+ 个模型**、重复条目变烦时，重构成"backends（连接层）+ models（复用 backend）"两层（参考 Codex model_providers）。当前 2-3 模型不值得改 schema |

## 已还清（保留记录）
- **D1 流式碎片拼接无测试** → M3 补 `tests/test_client.py`：碎片拼接、多工具、坏 JSON 兜底、usage、代理豁免均有直接单测。✅ 2026-07-02
- **D4 main 戳 Console 私有属性** → 加 `Console.input()` 收口，main 改用之。✅ 2026-07-02
- **D2 非流式死代码** → 删除 `complete`/`_normalize`/`LLMResponse`/`wants_tools`（生产与测试均不调用；ToolCall/_normalize_usage 保留）。✅ 2026-07-02
- **D3 EventKind 陈旧成员** → 从 Literal 移除 `"assistant"`（循环已不发、console 不处理）。✅ 2026-07-02

## 备注
- `context.py` 的"按消息数截断"不列为债——它是 M3 的**正式工作项**（token 感知截断），见 [m3-memory-plan.md](m3-memory-plan.md)。
