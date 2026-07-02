# 技术债登记册

> AI 迭代开发中，债务会隐形复利（LLM 在每个决策点埋入未言明的假设）。
> 这里显式追踪，防止"上次说的债"下次忘。每次里程碑评审更新本表。
> 最后更新：2026-07-02（M3 准入评审）

## 状态说明
- 🔴 高：影响正确性/安全，或脆弱的关键路径
- 🟡 中：影响可维护性，暂不影响功能
- 🟢 低：整洁度问题

## 登记表

| # | 债务 | 位置 | 级别 | 风险 | 计划 |
|---|------|------|:---:|------|------|
| D1 | 流式工具调用**碎片拼接无单元测试** | `llm/client.py` `_accumulate_tool_call`/`_finalize_tool_calls` | 🔴 | M2 最脆弱逻辑，仅手动验证过；改动易引入静默错误 | M3 期间补直接单测 |
| D2 | 非流式路径疑似**死代码** | `llm/client.py` `complete`/`_normalize`/`LLMResponse`/`wants_tools` | 🟡 | 生产与测试均不再调用，保留为"降级"但无人走 | M3 决定：删，或补一个降级测试证明其价值 |
| D3 | `EventKind` 含**陈旧成员** `"assistant"` | `agent/loop.py` | 🟢 | 循环已不发、console 不处理，误导读者 | 顺手清理 |
| D4 | main **戳 Console 私有属性** | `main.py:119` `console._console.input(...)` | 🟢 | 封装泄漏，Console 内部变更会波及 main | 加 `Console.input()` 方法收口 |
| D5 | **UI/CLI 层零测试** | `ui/console.py`、`main.py` | 🟡 | 最大文件(console 238行)无测试；confirm 解析、render_stream 状态机、SIGINT 仅手验 | 不必全补；M3 触及的部分(如持久化相关)要测 |

## 已还清（保留记录）
- （暂无）

## 备注
- `context.py` 的"按消息数截断"不列为债——它是 M3 的**正式工作项**（token 感知截断），见 [m3-memory-plan.md](m3-memory-plan.md)。
