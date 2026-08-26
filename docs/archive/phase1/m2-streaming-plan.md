# M2 实施方案 — 流式与过程透明

> 目标：解决"发问后长时间黑屏、思考/等待过程不透明"。
> 状态：待审阅，未动代码。对应 [ROADMAP.md](../ROADMAP.md) M2。
> 最后更新：2026-07-01

---

## 一、调研结论：显示什么，不显示什么

参考 Claude Code / Codex 的状态显示体系（todo-tracking、statusline、Terminal UI 架构等公开资料），
成熟 agent 的状态分三层。**核心原则：只显示我们 agent 真实产生的状态，不硬造需要额外能力支撑的显示。**

| 状态 | 成熟 agent | 我们现状 | M2 决定 | 依据 |
|------|:---:|------|:---:|------|
| spinner + 状态提示 | ✅ | 缺 | ✅ 做 | 覆盖网络/首 token 前空窗，证明"活着" |
| 流式 token 输出 | ✅ | 缺 | ✅ 做 | 根治黑屏，边生成边看 |
| reasoning 实时显示 | ✅ | 丢弃 | ✅ 做（开关）| DeepSeek 思考长，`show_reasoning` 默认折叠 |
| 当前工具调用提示 | ✅ | 已有 | ✅ 增强 | 已有"→ 调用工具"，加 spinner 表"执行中" |
| token 用量 | ✅ | 缺 | ✅ 做 | Loop Engineering 的"成本可见性" |
| `esc to interrupt` 中断 | ✅ | 缺 | ⏸ 不在 M2 | 需键盘监听+中断信号，复杂度高，留作紧邻小增强 |
| **TodoList 任务清单** | ✅ | 不具备 | ❌ 不做 | 依赖模型产出结构化 todo（专门工具+训练）；我们是纯 ReAct 单循环，硬做是假进度。属未来独立里程碑 |
| context 剩余百分比 | ✅ | 缺 | ❌ 延后 M3 | 依赖 token 感知上下文管理，M3 才做 |

**一句话**：M2 做「实时活动层」（spinner / 流式 / reasoning / 工具提示）+「成本层」（token 用量）；
不做「任务级进度层」（TodoList）——那需要我们尚不具备的模型能力，属过度设计。

---

## 二、状态机：M2 后一次任务的可见流程

```
[等待网络]  spinner「连接模型…」
    ↓ 首 chunk 到达
[思考中]    spinner「思考中…」；show_reasoning=on 时灰字滚动 reasoning
    ↓ 开始输出正文
[生成回复]  正文 token 实时逐段打印
    ↓ 模型请求工具
[调用工具]  「→ 调用工具 read_file(...)」+ spinner「执行中…」
    ↓ 工具返回
[观察结果]  工具结果（现有渲染）
    ↓ 循环回到「思考中」直到完成
[完成]      最终回复 Panel + statusline：本次 token 用量、耗时
```

---

## 三、技术实现（分步，每步可独立验证）

### 前置：先实测 DeepSeek 流式的碎片格式（用证据说话）
写一次性脚本，打印流式 chunk 里 `delta` 的结构，**确认工具调用参数如何跨 chunk 碎片化到达**、
usage 字段在何处。据此再写拼接逻辑，不靠猜。

### 步骤 1：`LLMClient` 增加流式方法（动 llm 层，不碰内核逻辑）
- 新增 `complete_stream(messages, tools)`，`yield` 归一化的增量事件：
  `reasoning_delta` / `content_delta` / `tool_call`（拼接完成后整体 yield）/ `usage`
- **核心坑点**：`tool_calls` 的 name/arguments 跨 chunk 碎片化，按 `index` 累积到缓冲区，
  流结束或该 index 完成时再产出完整 ToolCall。
- 保留原 `complete`（非流式）不动，供测试和降级用。

### 步骤 2：`AgentLoop.run` 消费流式（动内核，最小改动）
- 把「一次性 `complete` → 拿完整 response」改为「迭代 `complete_stream` → 逐个转发增量事件」。
- 新增 ItemEvent 类型：`reasoning`（思考增量）、`content_delta`（正文增量）、`usage`（token）。
- 循环控制流（终止条件、工具执行、历史写回）**保持不变**，只改"如何获得 response"。
- 保证：现有非流式测试仍可通过（用 `complete` 的路径或 mock 流）。

### 步骤 3：`Console` 流式渲染（动 UI 层）
- 用 Rich `Live` 做原地刷新：spinner + 状态词随阶段切换。
- 正文 `content_delta` 实时追加打印。
- `reasoning` 增量：`show_reasoning=on` 时灰字滚动，否则只保留 spinner。
- 工具调用：现有提示 + 执行中 spinner。
- 任务结束：statusline 显示 token 用量 + 耗时。

### 步骤 4：配置开关
- `config.yaml` 的 `agent` 或新增 `ui` 段加 `show_reasoning: false`（默认）。
- Pydantic schema 增字段；`Console` 据此决定是否滚动 reasoning。

---

## 四、动内核说明（铁律第 4 条）

M2 必须动 `agent/loop.py`——这是流式的本质要求，破例一次。约束：
- 只改「如何获得模型响应」（complete → complete_stream 迭代），**不改循环控制流**。
- 现有 28 测试全绿不回退；新增流式测试。

---

## 五、验收标准（对齐 ROADMAP M2）

1. 同一任务发问后 **3 秒内有可见反应**（spinner 立即出现）
2. `show_reasoning=on` 时思考过程实时滚动；`off` 时只见 spinner
3. 流式下工具调用**正确执行**（碎片拼接无误）——用真实多步任务验证
4. 云端 DeepSeek 与本地 LM Studio 都不卡黑屏（本地无 reasoning 时优雅降级为 spinner）
5. **token 用量可见**：任务结束显示本次消耗 token 数 + 耗时
6. 现有 28 测试全绿 + 新增流式相关测试

---

## 六、明确不在 M2 范围（避免膨胀）

- TodoList / 任务清单进度（需模型结构化 todo 能力，未来独立里程碑）
- `esc to interrupt` 中断（复杂度高，留作紧邻小增强 M2.5）
- context 剩余百分比（依赖 M3 的 token 感知上下文管理）

---

## 七、流式特有的失败模式与硬骨头（二次审视补充）

初版方案偏"顺利路径"。以下是流式真正会卡住实现的问题及对策，必须开工前想清。

### 7.1 流中途断开（已决策）
网络抖动/超时/后端 500 发生在"已打印部分 token"之后，已输出的收不回。
**决策：保留已输出内容 + 追加一行错误提示，该轮标记为失败。** 实现：
- `complete_stream` 捕获流迭代中的异常，`yield` 一个 `error` 增量事件（含已累积内容的说明），不抛出。
- `Console` 收到 `error` 时在当前输出后另起一行打印错误 Panel。

### 7.2 流式下的历史写回一致性（本质问题，比 UI 更重要）
非流式是"拿到完整 response 再 `add_assistant`"。流式下中途断开时，**已累积的部分内容仍要写回历史**，
否则模型下一轮失忆、或历史与用户所见不一致。对策：
- `AgentLoop` 在流式过程中累积 `content` / `tool_calls` 到局部变量。
- 无论正常结束还是中断，都用**已累积的内容**写回 `Conversation`（中断时内容可能残缺，但保持"所见即所存"）。
- 工具调用必须**拼接完成**才写回；未完成的半个工具调用丢弃，不写回。

### 7.3 工具调用碎片拼接失败的兜底
按 index 拼接后的 arguments 可能是坏 JSON（本地小模型高发）。
**复用现有 `_parse_arguments` 的容错逻辑**，不在流式路径重新实现。拼接得到原始字符串后交给它解析。

### 7.4 Live 渲染与流式打印的协调（已被"纯文本"决策大幅简化）
**决策：正文纯文本流式打印。** 因此 Live 只用于"无正文输出的空窗期"的 spinner：
- 等待网络 / 思考中（无正文）→ 用 `Live` 显示 spinner。
- 一旦开始有 `content_delta` → **停止 Live**，切换为直接 `print` 追加正文（纯文本）。
- 工具执行等待 → 再次用 `Live` spinner。
- 原则：**Live 与正文打印在时间上错开，绝不同时作用于同一区域**，从根上避免刷屏/错位。

### 7.5 chat 模式多轮 + 流式
每轮 `run` 结束时 Live 必须干净收尾（`Live` 用上下文管理器确保退出），
否则下一轮输入提示错位。测试需覆盖"连续两轮流式"。

### 7.6 Windows 终端流式表现
已踩过 GBK 编码坑。流式高频刷新在老 conhost 可能闪烁。
**验证项**：在目标终端实跑一次长输出任务，确认不闪、不乱码（纯文本方案已规避大部分风险）。

### 7.7 正文 markdown 渲染（已决策）
**决策：纯文本流式打印**，不做流式 markdown。代码块/标题不渲染，但绝不出错、不闪屏。
（未来若要美化，可作为 M2 之后的独立小优化，非本期范围。）

### 7.8 多工具调用的显示
一轮内模型可能请求多个工具。M2 **串行显示**：逐个"→ 调用工具 X" + 执行 spinner + 结果，
与现有非流式行为一致，不引入并行渲染复杂度。

---

## 八、测试策略（流式比非流式难测，需专门设计）

- **FakeStreamClient**：产出预设的 chunk 增量序列（含 reasoning、content、碎片化 tool_call、usage），
  驱动 `complete_stream` 和 `AgentLoop`，验证**事件顺序**与**拼接正确性**。
- 覆盖用例：
  1. 纯文本回复（无工具）流式事件顺序正确
  2. 工具调用碎片跨多个 chunk → 拼接出完整 ToolCall
  3. 碎片拼出坏 JSON → 复用 `_parse_arguments` 容错，不崩
  4. 流中途抛异常 → yield error 事件 + 已累积内容写回历史
  5. chat 连续两轮流式 → Live 干净收尾，无状态残留
- 保证现有 28 测试不回退（非流式 `complete` 路径保留）。

