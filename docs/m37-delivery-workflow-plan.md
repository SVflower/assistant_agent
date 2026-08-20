# M37 交付工作流演进方案

## 1. 决策

当前项目适合借鉴 DeepSeek Harness 的交付闭环，但不适合复制它的完整工具集合和插件运行时。

现有系统已经拥有统一 Tool Registry、权限/沙箱、Run checkpoint、Observability、受管 Output、Skill、
MCP、后台进程和 API/Web 映射。正确方向是在这些 owner 上补齐少量编排能力，而不是新增第二套 Session、
Job、Artifact 或权限状态机。

## 2. 第一批：TaskPlan（已实施）

- 新增模型工具 `update_task_plan(items)`，只接受完整任务表替换。
- 简单问答不创建计划；三个及以上可验收步骤的交付任务才使用。
- item 状态限定为 `pending | in_progress | completed`，顺序执行最多一项 `in_progress`。
- `RunCoordinator` 自动生成 revision/updated_at，模型不能伪造 revision。
- 计划直接写入既有 `RunObservabilitySnapshot.task_plan`，随下一工具完成 checkpoint 原子保存。
- API 使用既有 Observability DTO 映射；Web 使用既有 `taskPlan` 类型和 Store。
- Web Runtime 可以使用该工具，但不因此获得文件、Shell、进程或服务器目录权限。
- 工具按上下文预算动态注册；窗口不足时安全省略并同步移除提示词，不阻止 Runtime ready。
- Service/Event/Session/RunState/Observability 版本均不变。

## 3. 第二批：Deliverable Validation

在 M33 Native ArtifactWriter 之后增加确定性验证服务：

```text
Output capture -> type validator -> validation result -> bounded repair -> publish/fail
```

### M37-R2 已实施：发布前基础验证

- HTML：接受完整文档和合法片段；验证标签闭合、非空可展示内容并拒绝代码围栏包装。
- JSON：严格解析，拒绝 NaN/Infinity 和重复对象键。
- CSV：严格解析，验证非空唯一表头、行宽一致性以及列/行硬限。
- Markdown/纯文本：验证非空有效文本和 NUL 字符。
- 验证发生在草稿写完、正式 Artifact 原子发布之前；失败文件不会进入 Run、Session、API 或 Web。
- 成功/失败使用稳定 result_code 写入既有安全 trajectory，不保存正文、路径或解析器异常。
- 不新增模型工具、公共 DTO、契约版本、服务器权限或第二套验证状态机。

验证器属于 Runtime，不作为任意 Shell 暴露给 Web。验证结果进入安全 trajectory；正文、
服务器路径和原始异常不进入事件。取消/暂停沿用现有 Run 控制。

### M37-R3 已实施：一次有界修复

- 首次验证失败把稳定 reason code 与安全修改要求写回原 `create_output` 工具结果。
- Runtime 只允许一次重新生成；第二次失败产生唯一 failed terminal，不发布任何 Artifact。
- `validation_failures` 随 pending capture 写入 RunState v12，暂停或进程重启后不会重置重试次数。
- 修复轮继续消耗正常 iteration/context/model token 预算，不建立隐藏预算。

### 后续候选：浏览器验证

- 使用隔离浏览器验证 HTML 非空渲染、控制台错误和资源加载策略。
- 该能力需要独立资源配额和生产部署依赖评审，不能把 Playwright/Node 隐式塞入 Agent Core。

## 4. 第三批：Tool Result Retention（建议后续）

- 当前基线：Registry 已实施单次/累计输出预算与安全截断；Shell/Git 已把较大进程输出写入内部
  ArtifactStore，并向模型保留有界头尾预览。
- 小结果内联。
- 中等结果保留头尾与裁剪说明。
- 大结果写入既有受管 Artifact，并向模型返回 opaque ref。
- 定向分页读取，不把完整大结果重复放入上下文。

优先复用当前 ToolBudget、ArtifactStore 和 compaction，不建立新的 Spill 文件协议。不得在 Registry 中
把所有 MCP/Web 结果自动转换为服务器文件路径：Web Runtime 无权读取该路径，且会制造错误生命周期。
后续应由高价值工具提供受控分页，或先冻结不含 path 的临时结果 ref 与读取契约。

## 5. 暂不开发

- Goal：当前没有明确的跨多轮长期自治产品需求。
- Subagent/Workflow/Ralph：会显著扩大恢复、权限、费用、取消和并发状态面。
- 任意 Web Shell、任意服务器 Write/Edit：违反 Web Runtime 部署边界。
- 通用 Code Mode：需先完成本地/API 模型能力矩阵和 Eval，不能默认启用。
- 新插件框架：现有 Bootstrap/Registry/Skill/MCP 已覆盖近期扩展需求。

## 6. 验收原则

每批能力必须满足：状态唯一 owner、重启可恢复、取消可终止、Web 不扩权、上下文有硬限、API 不解析
Agent 私有文件、前端不从自然语言猜状态，并使用真实验证结果而不是模型自述作为完成依据。
