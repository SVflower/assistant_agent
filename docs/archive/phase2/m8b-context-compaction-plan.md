# M8b — 上下文进化（摘要压缩替代硬截断）

> **状态：✅ 已交付**。四条评审要点全部落地：① 双历史（raw + checkpoint{summary,covered_upto} + tail）；
> ② checkpoint 随 Session 持久化，resume 不重复摘要；③ 压缩边界按完整用户轮（group_turns）；
> ④ 摘要 token 经 ItemEvent(usage) 独立上报，不进 M6.5。Compactor 持 client、loop 编排触发，
> context 保持被动。**默认关闭，关闭时上下文逐字节等于硬截断**。摘要失败→降级硬截断，会话不断。
> 实测真实模型压缩一段 6 轮历史，摘要正确保留关键约束（x0-x3 赋值）；240 测试绿。

## 解决什么

当前上下文管理是**硬截断**（`src/assistant_agent/agent/context.py`，行号为归档时值）：token 预算装不下
最旧消息时直接丢弃，长任务里丢掉早期关键决策/约束/结论，模型"失忆"。

M8b 用**摘要压缩**替代硬丢：历史逼近预算时，把最旧一段消息交给模型压成简短摘要，以摘要顶替原文，
保留语义、腾出 token。核心目标：长会话不失忆。前置 M8a 提供准确的"当前占用"读数。

## 现状锚点（开工前必读当前代码）

- `Conversation._truncated()`：消息数硬上限 + token 预算，从新往旧保留，丢开头孤立 tool 消息。
- `messages()` = `[system, *_truncated()]`；`export_history()` 导出未截断原文。
- M8a 已把 tools schema + reserved_output 计入预算口径。

## 双历史状态模型（评审核心补充）

维护三段清晰状态，避免"摘要游标/恢复机制未定义"：

```
raw_history            完整原始历史（永不删，存档用）
compaction_checkpoint  { summary: 摘要文本, covered_upto: 已覆盖到的 raw 消息游标 }
recent_tail            covered_upto 之后的完整轮次（未压缩）
```

- 发给模型的 `messages()` = `[system, summary_msg(checkpoint), *recent_tail]`。
- **checkpoint 随 Session 持久化**：否则 `--resume` 长会话会重新摘要、重复烧 token。
- 多次压缩：新摘要并入旧 checkpoint（summary 合并、covered_upto 前移）。

## 范围（本期做 / 不做）

**做：**
- 触发式压缩：M8a 口径下历史占用超阈值（预算的 X%）→ 压最旧一段。
- **压缩边界按完整用户轮次分组**（不只保 assistant↔tool 配对）：一个"用户轮"= 用户消息 +
  其引发的 assistant/tool 往返；边界落在轮与轮之间，不切碎一轮。
- 双历史模型（上节）+ checkpoint 随 Session 持久化。
- 摘要 token **独立计入全局 usage**（下"用量"），不混入 M6.5 工具预算。
- 压缩失败降级：摘要调用失败/超时 → 回退硬截断，会话不中断。
- 配置 `agent.compaction`（enabled、threshold、keep_recent_turns、summary_model 可选）。

**不做（本期）：**
- 语义检索式记忆（RAG/向量库）——更大方向，信号驱动。
- 跨会话记忆压缩。
- 工具结果选择性压缩（先整段摘要，够用再细化）。

## 关键设计点

**1. 触发与分段**
- 触发：M8a 的 `context_used > 预算 * threshold`（如 0.8）。
- 保护窗：最近 `keep_recent_turns` 个完整用户轮，绝不压缩。
- 待压段：保护窗之前、system 之后的最旧若干完整轮。

**2. 摘要生成（禁工具、独立超时）**
- 用当前 client（或配置的 summary_model，引用已有 provider 名，**不在业务逻辑硬写模型**）。
- 请求"把以下对话压成要点，保留决策/约束/结论/待办"；**禁用工具**、设独立超时。
- 产出 summary 文本，更新 checkpoint。

**3. 摘要用量核算（评审：不算 M6.5）**
- M6.5 管的是**工具调用数 + 工具输出字符**，不管 LLM token。摘要是 LLM 调用，不能塞进去。
- 摘要请求的 in/out token 计入**全局 usage**，经 `ItemEvent(kind="usage")` 或独立日志事件上报。
- 关联 M8a：M8a 的 usage 口径要能容纳"非工具的内部 LLM 调用"。

**4. 内核触碰（重点，极谨慎）**
- 改 `agent/context.py`（rank 3 内核）——真·内核改动，非 M7 轻碰。
- 压缩逻辑抽 `agent/compaction.py` 的 `Compactor`；context 调它；**压缩关闭时行为逐字节等于现状**。
- 压缩要调模型 → 依赖方向：**不让 context（rank 3）反向依赖 llm（rank 1）之上**。倾向由 loop 编排：
  loop 在每轮开始检查阈值、调 Compactor（持有 client）、把结果写回 conversation。开工前先定死依赖方向、过架构测试心智模型。

**5. 降级与幂等**
- 摘要失败/超时/空 → warning + 回退硬截断，会话继续。
- 压缩后仍超（摘要太长）→ 对摘要再截断兜底，绝不撑爆窗口。

## 安全 / 正确性

- 摘要有损——UI/`/context` 明示"早前对话已压缩为 N 段摘要"，用户知情。
- 存档（raw_history）永远完整，压缩只影响发给模型的视图——误压可从存档恢复。
- checkpoint 持久化到 Session，`--resume` 不重复摘要。

## 文件改动清单

**新增：**
- `src/assistant_agent/agent/compaction.py`：`Compactor`（分轮、摘要、降级、checkpoint 合并）。
- `tests/test_compaction.py`。

**修改：**
- `agent/context.py`：接入双历史 + Compactor（关闭时零行为变化）。
- `agent/loop.py`：每轮开始按阈值触发压缩（编排，持 client）。
- `session/store.py`：Session 持久化 compaction_checkpoint。
- `config/schema.py`：`CompactionConfig` 挂到 AgentConfig。
- `ui/console.py` + `/context`：显示已压缩段数。
- `config.example.yaml`：compaction 段（默认关或保守开）。
- `tests/test_architecture.py`：确认新增依赖边不破分层。

## 测试计划

1. **不触发**：占用小于阈值 → messages 与现状完全一致（回归）。
2. **触发压缩**：超阈值 → 最旧轮被替换为摘要、保护窗原样、system 在最前。
3. **按轮不切碎**：待压段边界落在完整用户轮之间，不切碎一轮的 assistant↔tool。
4. **checkpoint 持久化**：存档后 resume，不重新摘要（covered_upto 恢复正确）。
5. **降级**：摘要调用失败（fake client 抛错）→ 回退硬截断、不崩。
6. **存档完整**：export_history / raw_history 仍是未压缩原文。
7. **摘要超长兜底**：摘要仍超预算 → 再截断不撑爆。
8. **用量独立**：摘要 token 计入全局 usage，**不**计入 M6.5 工具预算。
9. **关闭开关**：enabled=false → 逐字节等于现状硬截断。
10. **架构**：依赖方向不破 _LAYER_RANK。
11. **回归**：现有全绿。

## 验收标准

1. 长会话超阈值自动压缩，早期决策/约束以摘要保留，不再整段丢。
2. 压缩关闭时行为与现状逐字节一致（内核回归保护）。
3. 摘要失败优雅降级，会话不中断。
4. checkpoint 随 Session 持久化，resume 不重复摘要。
5. 摘要 token 计入全局 usage，不污染 M6.5 工具预算。
6. 存档保完整原文；`/context` 可见已压缩状态。
7. 依赖方向不破坏架构分层测试。
8. 新增测试全绿，ruff + 架构测试通过；真实模型验证一次长会话压缩（或如实记录阻塞）。

## 顺带（交付后）

- TECH_DEBT：登记"选择性/检索式记忆"为未来方向。
- 状态文档同步（DoD 第 6 条）。
