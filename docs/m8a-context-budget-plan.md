# M8a — 上下文预算口径（还 D10）

> **状态：评审新增里程碑**。评审指出 M7b 会把大量 MCP 工具 schema 注册进来，而当前预算不计 schema
> （[D10](TECH_DEBT.md)），窗口小的本地模型会静默溢出。M8a 先统一口径，给 M7b 硬防护和 M8b 压缩铺路。

## 解决什么

当前 token 近似只算 `system + messages`（[context.py:100](../src/assistant_agent/agent/context.py#L100)），
**不计工具 schema**（默认内置约 3454 字符，D10）。M7b 接入 MCP 后工具 schema 可能翻几倍。
结果：`/context` 占用读数偏低、截断预算偏松，小窗口模型比配置预算更早溢出且无预警。

M8a 把"发给模型的全部东西"纳入统一口径：`system + tools schema + messages + reserved_output`。

## 与 M7b/M8b 的关系（解耦）

- **M7b**（先做）：用**数量/长度硬上限**兜底防 schema 爆炸（不依赖精确 token）。
- **M8a**（本里程碑）：补**精确 token 口径**，让 `/context` 与截断预算反映真实占用。
- **M8b**（后做）：用 M8a 的准确"当前占用"读数决定何时触发摘要压缩。

## 范围（做 / 不做）

**做：**
- token 估算计入 tools schema：`registry.schemas()` 序列化后按现有字符/4 口径估算。
- 预留输出预算 `reserved_output_tokens`：从可用预算里扣掉给模型回复的空间。
- 统一预算 = `max_context_tokens - reserved_output - system - tools_schema`，剩下给 messages。
- `/context` 分项显示：system / tools / messages / reserved 各占多少、总占用%。
- usage 口径能容纳"非工具的内部 LLM 调用"（为 M8b 摘要用量铺路）。

**不做：**
- 精确分词器（仍用保守的字符/4 近似——宁可高估多截，不撑爆）。
- 按模型定制 reserved（先全局一个配置值，够用）。
- 摘要压缩本身（M8b）。

## 关键设计点

**1. schema 计入**
- context 需要拿到当前工具 schema 才能估算。依赖方向：不让 context 反依赖 registry，
  由 loop 把 `registry.schemas()` 的估算值（或 schema 列表）传入 context，或 context 持一个只读估算钩子。
  开工前定死方向、过架构测试心智模型（与 M8b 同一约束）。

**2. reserved_output**
- 模型回复也占窗口。可用消息预算 = 总窗口 − reserved − system − tools。
- reserved 太小→回复被截；太大→历史留太少。给保守默认（如 1024），可配。

**3. 兼容 M6.5**
- M6.5 的工具输出预算不变（那管工具结果字符）。M8a 只改"上下文 token 口径"，两者正交。
- usage 上报扩展：除工具调用外，也能记内部 LLM 调用（摘要）的 token —— M8b 会用。

## 文件改动清单

**修改：**
- `agent/context.py`：预算计算计入 tools schema 估算 + reserved_output。
- `agent/loop.py`：把 tools schema（或其估算）传给 context。
- `config/schema.py`：`AgentConfig` 加 `reserved_output_tokens`（默认 1024）。
- `ui/console.py` + `/context`：分项显示 system/tools/messages/reserved。
- `obs/logger.py`（如需）：usage 事件容纳内部 LLM 调用口径。
- `tests/test_context.py`（或新增）：口径测试。
- `docs/TECH_DEBT.md`：D10 标记还清。

## 测试计划

1. **schema 计入**：注册工具后，可用消息预算 = 总窗口 − system − tools − reserved（数值断言）。
2. **schema 影响截断**：工具多→消息预算变小→更早截断（对比无工具基线）。
3. **reserved 生效**：reserved 增大→消息预算相应减小。
4. **零工具回归**：无额外工具且 reserved=0 时，行为与现状逐字节一致。
5. **/context 分项**：显示 system/tools/messages/reserved 且总和正确。
6. **架构**：新增依赖边不破 _LAYER_RANK。
7. **回归**：现有全绿。

## 验收标准

1. `/context` 占用反映真实（含 tools schema + reserved），MCP 大量工具时不再偏低。
2. 截断预算计入 schema，小窗口模型不再静默溢出。
3. reserved_output 保证模型有回复空间。
4. 口径关闭/归零时与现状一致（回归保护）。
5. D10 还清；不破坏架构分层。
6. 新增测试全绿，ruff + 架构测试通过。

## 顺带（交付后）

- TECH_DEBT：D10 标 ✅；若引入按模型定制 reserved 的需求，登记为信号驱动项。
- 状态文档同步（DoD 第 6 条）。
