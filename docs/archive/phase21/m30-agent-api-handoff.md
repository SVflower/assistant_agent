# M30 Agent -> API/Web 交接

## 固定依赖

- Agent commit：以 M30 功能分支最终完整 commit 为准，不固定移动分支名。
- `EVENT_CONTRACT_VERSION = 1`：不变。
- `SESSION_CONTRACT_VERSION = 3`：不变。
- Run checkpoint schema：v7，不变。
- ChartSpecV1/V2、ChartArtifactV1/V2 字段：不变。

## API 需要处理

`tool_result` 为 `artifact_rejected` 时，`result_metadata` 现在可能包含以下 additive 安全字段：

```json
{
  "field_path": "panels[0].aggregate",
  "allowed_values": ["count", "sum", "mean", "min", "max"],
  "duplicate_coordinate": ["08:00", "A"],
  "duplicate_count": 2,
  "correction_remaining": 1
}
```

- 所有字段均可缺失；旧 Agent/其他校验错误仍可能只有通用 metadata。
- API 只保真映射白名单字段，不解析中文错误文本，不发送原始工具参数。
- `retryable=true` 不授权 API 自动重试；aggregate 必须由模型依据业务语义明确选择。
- `correction_remaining` 只描述当前图表意图，不能作为 Run 重试或网络重放额度。
- `artifact_rejected` 仍是局部失败，不得合成 failed terminal。

## Web 需要处理

- 新建 Heatmap Artifact 的 `panels[].x_axis.scale` 和对应 `y_axes[].scale` 都是 `category`。
- categories 从 series 指向的 derived dataset X/Y 列确定，value 列为有限 number。
- 空/损坏/历史异常 Heatmap 继续局部降级，不影响文字消息与 Run terminal。
- 可选择展示 field_path 和 allowed_values 作为诊断，但不得替用户或模型选择 aggregate。

## 事件序列

首次语义错误后修正成功：

```text
tool_call(present_chart)
tool_result(is_error=true, result_code=artifact_rejected,
            result_metadata.correction_remaining=1)
notice(图表未创建)
tool_call(present_chart, corrected aggregate)
tool_result(is_error=false, chart=ChartArtifactV2)
final(text)
run_terminal(completed)
```

连续无效：第二次错误的 `correction_remaining=0`，随后模型继续文字回答。Agent 不新增 EventKind，API
不复制纠错计数或 checkpoint 状态机。

## 联调验收

1. 单面板和多面板 Heatmap 均能渲染，Y 轴不再按 linear 解释分类字符串。
2. 重复坐标首错 metadata 保真，修正后成功。
3. 空 rows、全 null 和空白分类只丢图，不丢文字和 terminal。
4. 旧 V1/V2 Artifact、Session 恢复和未知 metadata 键保持兼容。
