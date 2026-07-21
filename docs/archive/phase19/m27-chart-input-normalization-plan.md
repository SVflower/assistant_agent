# M27 图表模型输入归一化方案

> 状态：已实施。日期：2026-07-21。

## 问题

`present_chart` 过去直接向模型暴露严格 `ChartSpecV1` schema。本地小模型能够选择工具，
但常遗漏 `columns[].data_type`，Registry 会在工具执行前拒绝，模型随后可能重复提交同一错误。
Cloud 模型通常能提交完整参数，但不应形成两条 provider 专属路径。

## 决策

- 公共 `ChartSpecV1`、`ChartArtifact`、`StepEvent` 和 Event v1 保持不变。
- 只在工具输入边界接受受控草稿：`data_type` 可省略，其余 JSON Schema 继续禁止额外字段。
- 缺失类型按列值确定性推断：有限数字为 `number`，明确 ISO 日期为 `datetime`，普通文本为
  `string`；全 null、混合类型、日期与普通文本混合、bool、NaN/Inf 和复杂值拒绝。
- 草稿归一化后必须再次通过严格 `ChartSpecV1`，随后才计算 hash、持久化 Artifact 和发送事件。
- 第一次无效结果允许一次定向修正；失败标记已在 Run checkpoint 的工具消息中持久化，恢复后
  不重置次数。第二次失败不可重试，但不影响文字 final 和唯一 terminal。
- 不解析 ECharts option，不接受 formatter、HTML、URL、graphic、script/function、`__proto__`
  或任意执行代码。
- Provider 是否为 cloud/LM Studio 不参与决策；不支持原生 tool calling 的模型继续安全降级文字。

## 受影响文件

- `tools/chart_input.py`：草稿归一化与安全错误。
- `tools/charts.py`：模型 schema、一次修正策略和严格契约入口。
- `tools/tool.py`、`tools/registry.py`：领域工具可收敛通用参数错误。
- `tools/context.py`、`agent/run/coordinator.py`、`application/runtime.py`：注入 checkpoint 驱动的
  已持久化结果计数。
- `agent/prompts.py`：紧凑合法示例。
- 图表单元/服务/契约测试与 deterministic eval。

## 验收

- 严格参数和缺类型草稿均成功；number/string/datetime 推断确定。
- 歧义、越界、未知引用和恶意字段 fail closed，错误不回显原始值。
- 一次修正跨 checkpoint 恢复有效；连续失败仍有 final 和唯一 completed terminal。
- Ruff、mypy、import-linter、全量 pytest+coverage、scripted/recovery eval 全绿。

## 公共服务契约影响

无。输入草稿 schema 只属于模型工具调用边界；公共 `ChartSpecV1` 和 Agent 到 API 的事件字段、
版本、Artifact hash 与恢复格式均未改变。API/Web 不需要识别模型类型或新增兼容分支。
