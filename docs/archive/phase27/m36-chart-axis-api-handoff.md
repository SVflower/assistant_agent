# M36 Agent -> API/Web 交接

## 固定范围

- Agent 实现提交：以交付报告中的本地 main 完整 commit 为准。
- 公共版本不变：ChartSpec v2、Service v5、Session v5、RunState v11、Event v1。
- 未新增 DTO 字段、事件种类或 renderer 配置；未修改 Agent Loop。

## Agent 语义

1. `DatasetColumnV1.label` 与可选 `unit` 被确定性投影到 canonical
   `panels[].x_axis.title` / `panels[].y_axes[].title`，单位格式为 `名称（单位）`。
2. `datetime` X 轴映射为 `time`；普通类别（包括数值子组号）映射为 `category`；scatter/bubble
   的数值 X 映射为 `linear`。
3. 双轴与多面板按各 series 实际绑定列生成独立标题；同轴同单位系列合并标题。
4. Histogram Y 为“频数”，boxplot 使用原始测量列，percent stack Y 为“占比（%）”，heatmap X/Y
   使用两个分类列标题。
5. UCL/LCL/CL、目标值等 reference line label 不参与轴单位推断。

## API 必做

1. 保真传输 ChartSpecV2 的 `datasets[].columns[].unit`、`panels[].x_axis.title/scale` 和
   `panels[].y_axes[].title/scale/position`，不得丢弃或重算。
2. 不新增网络 DTO 或事件映射，不解析标题来猜单位；原始结构化单位仍以 column `unit` 为准。
3. 未知/损坏图表继续只降级图表，不影响文字、final 或唯一 terminal。

## Web 必做

1. 显示 canonical X/Y 轴标题；标题已含展示单位，Tooltip/表格仍可使用 column `unit`。
2. 在每次容器 resize、面板切换和全屏切换后，基于实际像素宽高、轴 scale、tick 数量、最长标签
   测量宽度、面板数量和 legend 占位重新计算 tick interval、rotation 与 grid 边距。
3. 优先保持标签可辨识：短标签水平显示；密集长标签先抽样，再按受控角度旋转；不得默认全部竖排，
   也不得为未知标签预留固定大块空白。
4. 左右轴标题和 reference line/band label 都纳入边距测量。右侧不足时使用固定实现的 inside、clip 或
   省略策略，避免标签裁切、溢出或撑坏工作台。
5. renderer 只从白名单 canonical 字段构建 option。禁止 Artifact/model 注入 `grid`、`formatter`、
   HTML、URL、JS/function、任意 style；布局算法属于 Web 实现，不反向写入 ChartSpec。

## 联调验收

1. datetime 趋势图显示时间 X 轴标题和带单位 Y 轴标题。
2. 数值子组号的 SPC 图仍使用 category X 轴；均值/极差子图各自显示正确名称和单位。
3. dual-axis 左右轴标题和单位独立；reference line 的 UCL/LCL/CL 不成为单位。
4. 20/50/100 个长时间标签在窄卡片、宽屏、双面板和全屏下均不裁切工作台，布局随容器变化。
5. 右轴 reference label 不被容器裁掉；Tooltip、图例、缩放、表格切换和下载行为不回退。
