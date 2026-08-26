# M28 ChartSpecV2 高频普通图表方案

> 状态：已实施。日期：2026-07-22。未修改 `agent/loop.py`。

## 目标与边界

在逐字节兼容 `ChartSpecV1` canonical JSON/hash 的前提下新增受控 `ChartSpecV2`，覆盖高频 L4
普通图表。V2 采用 `datasets + layout + panels + derivations`，模型仍只提交紧凑草稿，不能提交
ECharts option、formatter、HTML、URL、graphic、JS/function 或任意 style 对象。

支持 line、area、bar、grouped_bar、stacked_bar、percent_stacked_bar、pie、donut、
combo_bar_line、dual_axis、scatter、bubble、histogram、boxplot、heatmap，以及 reference line/band、
error bar、annotation、双 Y 轴和最多四面板。本期不做 KDE、小提琴图、SPC、控制限或工业分析。

## 设计

- `TabularDatasetV1` 是中立数据集契约，可由未来 `IndustrialAnalysisArtifact` 复用；本期不实现 SPC。
- `ChartSpecV2` 是严格、冻结、禁止额外字段的 canonical DTO；V1/V2 Artifact 通过
  `schema_version` 判别联合。
- `present_chart` 只暴露紧凑 draft，不暴露 canonical discriminated union 或巨大 `oneOf`；cloud
  与 LM Studio 使用同一路径，继续沿用 M27 一次定向修正、第二次停止。
- histogram 由 Agent 对原始有限数值确定性计算：显式 1..100 bins；自动 Freedman-Diaconis，
  IQR=0 回退 Sturges；左闭右开，末 bin 含右端。
- boxplot 由 Agent 使用 Type-7 quartile、1.5 IQR whisker 计算，并把原始 outlier 放入独立 derived
  dataset。percent stack、pie/heatmap 聚合也由 Agent 确定性完成。
- 安全硬限：512 KiB/Artifact、16 Artifact/Run、2 MiB/Run；4 datasets、4 panels、2 layout columns、
  12 columns/dataset、5000 rows/dataset、20000 cells、8 series；overlay 数量按冻结契约限制。
- `ItemEvent.chart`、ToolResult、Run/Session snapshot 使用 V1/V2 联合；Event 外壳保持 v1。
- Run checkpoint 升 v7，v1-v6 无损迁移；Session schema/contract 升 v3，v1/v2 锁内原子迁移。

## 主要文件

- `contracts/datasets.py`：中立 `TabularDatasetV1`。
- `contracts/charts_v2.py`：V2 canonical DTO、交叉校验、Artifact/hash。
- `contracts/presentation_common.py`：V1/V2 共用且不改变 V1 字节的 canonical helper。
- `tools/chart_input_v2.py`：小模型友好草稿归一化。
- `tools/chart_transforms.py`：确定性 histogram/boxplot/percent/aggregate。
- `contracts/charts.py`、events/session/run/service/persistence：版本化联合、迁移、恢复、fork。

## 验收

1. 15 种图表、overlay、多轴、多面板均通过严格 DTO 与 Artifact round-trip。
2. histogram/boxplot golden、percent/aggregate 安全边界确定且跨模型一致。
3. V1 canonical hash 固定回归；Event v1 外壳不变。
4. Session v2 -> v3、Run v1-v6 -> v7、V2 Artifact fork 深复制可恢复。
5. 恶意字段、歧义值、负 bubble、非法 percent、重复 pie/heatmap fail closed。
6. 图表局部失败不吞文字 final，不改变唯一 run terminal。
7. Ruff、mypy、import-linter、pytest+coverage、scripted/recovery eval 全绿。

## 风险与技术债

V2 增加 API/Web renderer 工作量，但不引入任意渲染配置。未来工业分析只复用 Dataset/受控 renderer，
不得把普通 reference line/band 解释成控制限。M28 未新增已知技术债；主目录技术债文档有用户未提交
修改，本分支为保护其内容不改该文件。
