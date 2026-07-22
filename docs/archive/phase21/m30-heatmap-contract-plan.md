# M30 Heatmap 契约与纠错计划

## 背景

真实运行暴露两类问题：重复 Heatmap 坐标缺少 aggregate 时，小模型收到泛化错误后没有修正；已生成
Artifact 的 Y 分类数据却声明 linear axis，Web 可得到合法 DTO 但无法稳定渲染。

## 范围

- Heatmap 新建路径固定生成 X/Y 两个 category axis。
- 拒绝空 rows、全 null value、null/空白分类坐标和空 derived dataset。
- 重复坐标继续要求调用方明确选择 count/sum/mean/min/max，Agent 不猜业务语义。
- 多面板错误提供 field_path、候选值、重复坐标/数量和剩余修正次数。
- 修正额度按图表意图隔离，并从既有 checkpoint 消息账本恢复。
- cloud 与 LM Studio 继续使用相同 schema、提示词和归一化路径。

## 兼容边界

- 不修改 `agent/loop.py`。
- Event v1、Session contract/schema v3、RunState v7、ChartSpecV1/V2 DTO 均不升版。
- `ToolResult.metadata -> StepEvent.result_metadata` 只增加可选键。
- 不收紧历史 V2 Artifact 的公共解析，否则 M28 已保存的 linear-Y Heatmap 可能拖垮 Session/Run 加载。
  严格约束位于 Agent 唯一新建入口，生成后的 canonical 字段有契约测试。
- 图表失败不改变 final/run_terminal。

## 测试

- 单/多面板 category axes 与合法 canonical 字段。
- 空 rows、全 null、null/空白坐标、重复坐标聚合。
- 多面板结构化 metadata 和安全错误文本。
- 一次修正、不同图表意图隔离、checkpoint 恢复。
- 省略 data_type 的本地模型输入。
- scripted Heatmap 修正轨迹、全量 pytest/coverage、Ruff、mypy、import-linter、recovery eval。
