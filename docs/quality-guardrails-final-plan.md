# 质量护栏与运行时边界 — 最终实施计划

> 状态：待实施（方向已双方评审确认）
> 最后更新：2026-07-13
> 来源：Codex 原案 [quality-guardrails-plan.md](quality-guardrails-plan.md) 与本会话评审，
> 双方结论已合并入此执行版。本文档是唯一执行依据。

## 共识总纲（三处分歧已定案）

1. 行数：不删自动检查，改 **300 警告（不 fail）+ 500 硬上限（fail）**。
2. eval：不进本次，登记为**触发型后续债**。
3. 工具输出截断：**单次截断提到一期、只改 `registry.execute`，loop 零触碰**；
   跨轮累计预算留二期。

不可妥协红线（贯穿始终）：provider 必经 `llm/client.py`；loop 不依赖 UI、工具不反向
依赖 agent/ui；执行集中经 `ToolRegistry.execute`；**改 `agent/loop.py` 前必先确认**。

## 第一期：不动内核，按序实施（S1→S5）

> 每步独立可提交、独立可测。全程 `agent/loop.py` 一行不改。

### S1 文档表述澄清（纯文档）
- `CLAUDE.md` / `AGENTS.md` 铁律4："内核保持封闭 / 不改循环"
  → "内核**职责**稳定、实现允许**受控演进**（改动前先确认、改后测试不回退）"。
- README / DESIGN 去掉"永远不动"绝对措辞，保留"动 loop 先确认"闸门表述。
- 测试：无（文档变更，靠人审）。

### S2 架构测试改分级软/硬（改 `tests/test_architecture.py`）
- 常量：`_SOFT_LINES = 300`、`_HARD_LINES = 500`。
- `test_file_size_budget`：超 300 → `warnings.warn`（不 fail）；超 500 → assert 失败。
- 依赖方向 / 内核 UI 无关 / 工具不反向依赖 三项**保留强化**，不动。
- 注释写明：300 是交人评审的重构信号，500 是交 CI 的防膨胀刹车。
- 测试：本步即测试文件自身；跑 `pytest tests/test_architecture.py` 全绿。

### S3 D8① 时间分离（改 `tools/base.py` + `tools/registry.py` + `obs/logger.py`）
- `ToolContext` 加一次性字段 `_last_approval_wait_ms: int | None = None`。
- `request_confirm`：用 `perf_counter` 测 `confirm()` 回调墙钟耗时写入该字段
  （命中永久允许记忆、无回调时不写）。
- `registry.execute`：读该字段 → 传给 `logger.tool_call(approval_wait_ms=...)` →
  **立即清零**，不残留到下次。
- `EventLogger.tool_call` / `NullLogger.tool_call`：加可选 `approval_wait_ms: int | None = None`，
  为 None 时不写进事件。
- 事件：`{"type":"tool_call",...,"duration_ms":42,"approval_wait_ms":8800}`。
- 测试（`test_obs.py`）：有确认→两字段分离且 duration 近零等待；无确认→无 approval 字段；
  拒绝确认→仍有 confirm＋tool_call。

### S4 D8② 递归脱敏（改 `obs/logger.py`）
- 新纯函数 `_sanitize_value(value, max_chars, key_hint=False)`：
  dict 按键名命中敏感词整体遮蔽否则递归；list 递归每元素；
  str 已知前缀（sk-/ghp_/AKIA/xox…）遮蔽再截断；其他标量原样。
- `tool_call` 的 args 改用它，替换现有只处理顶层的 `_sanitize_args`。
- 测试（`test_obs.py`）：嵌套 dict、list、`multi_edit.edits[].new_string` 里的 token、
  敏感键名，均被遮蔽；每字符串仍受 `max_payload_chars`。

### S5 工具单次输出截断（改 `config/schema.py` + `tools/base.py` + `tools/registry.py`）
- `AgentConfig` 加 `max_tool_output_chars: int`（默 8000，`ge=0`；0＝不截断）。
- 值经 `ToolContext.max_tool_output_chars` 注入（main 构建时从 config 传）。
- `registry.execute`：记日志后、返回前，若 `result.output` 超限则截断并追加
  `…（已截断 N 字符，可缩小范围重试）`。UI＋上下文同拿截断版。
- 审计事件加 `truncated: true`（`output_len` 仍记原始长度）。
- 测试（`test_tools.py` + `test_config.py`）：超限截断＋标记、未超限不动、默认值、
  YAML 覆盖与下界。

## 第二期：改 `agent/loop.py`，须再次确认后才动

> 一期完成、跑顺后单独开工。动内核前按红线**先向用户确认**。

- `AgentConfig` 加 `max_tool_calls`（单任务工具调用总数，默 ~50）。
- `ToolRegistry` 加任务级生命周期 `begin_task()` / `end_task()`：
  - 每次执行前检查调用数预算，超限返回结构化、模型可理解的错误结果。
  - 跨轮累计输出字符预算（在 S5 单次截断之上叠加总量控制）。
- `AgentLoop.run()` 任务始末调用预算生命周期；耗尽时发清晰错误并安全终止，
  复用现有"批次前检查、不中途中断"约束，**不留悬空 tool call**。
- 测试（`test_loop.py`，fake client）：单轮多调用、跨轮累计、截断、边界终止、会话一致性。

## 待定默认值（须实测标定，不拍脑袋）

- `max_tool_output_chars`（一期，默 8000）：用本地 LM Studio 跑读大文件/长 shell，
  确认不误伤正常任务、又挡住上下文吞噬后定稿。
- `max_tool_calls`（二期，默 50）：用首批真实任务观察正常上限后收紧。

## 降为触发型后续债（登记 TECH_DEBT，本次不做）

- **eval 任务集**：触发＝需对比模型 A/B 完成度、或回归"越权调用/越预算"行为时。
  届时用 YAML 案例（id/task/tools/expect/budget）＋ fake-client runner，不接真实模型 CI。
- **D8 剩余项**：`/clear` 换会话、`/model` 切模型后日志元信息不更新（原 D8 ③④）。

## 验收标准

**第一期**（S1–S5 全绿）：
1. 架构测试仍挡错误依赖；文件超 300 仅警告、超 500 才失败。
2. 文档允许受测保障的内核演进，仍禁 UI/业务侵入 loop，保留"改 loop 先确认"闸。
3. 嵌套参数密钥被脱敏；日志区分 `approval_wait_ms` 与 `duration_ms`。
4. 单次工具输出超限被截断＋标记，上下文有界；**`agent/loop.py` 未改**（git diff 证）。
5. D8 ①② 标记已解决；eval 与 D8 ③④ 登记为触发型债务。
6. `pytest` / `pytest --cov` / `ruff format` / `ruff check` 全绿；无密钥/产物入库。

**第二期**（另行确认后）：上"第二期"四点均满足，循环/会话/工具测试无回归。

## 风险与取舍

- 行数软/硬：500 刹车防退化为无约束，300 警告仍提示评审。
- 输出截断落点：选 `registry.execute` 一处截断（UI＋上下文同版）——不碰 loop、单点收口、
  终端不被超长输出刷屏。代价：UI 也只见截断版；标记提示"缩小范围重试"保留恢复路径。
  若日后要"UI 全量、仅上下文截断"，再动 context 层归二期。
- 二期预算默认值：过小误伤、过大失效 → 必须实测标定。
- 脱敏尽力而为：`logging.log_tool_io=false` 仍是敏感任务可靠兜底。

## 实施顺序建议

S1（文档）→ S2（架构测试）→ S3（时间分离）→ S4（递归脱敏）→ S5（输出截断）。
S1–S2 风险最低可先落地；S3–S5 每步独立测试独立提交。一期全程不碰内核。
