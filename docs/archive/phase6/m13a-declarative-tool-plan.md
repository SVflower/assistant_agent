# M13a 声明式工具适配层方案

> 状态：已完成（2026-07-17）。未修改 `agent/loop.py`，未迁移现有工具。

## 背景

当前工具通过继承 `Tool`、手写 JSON Schema、实现 `run(args, ctx)` 接入。该契约稳定且已承载
参数校验、权限、预算、审计、输出治理和恢复语义，但新增简单工具时存在类型注解与 JSON Schema
重复维护的问题。

本里程碑借鉴 smolagents 的声明式工具体验，在现有工具内核上增加可选适配层，不替换现有
`Tool`，不改变 Registry、权限和 Agent Loop。

## 范围

### 必做

- 新增 `assistant_agent.tools.declarative`：
  - `@agent_tool` 装饰器；
  - `FunctionTool` 适配器；
  - 从 Python 类型注解生成 JSON Schema；
  - 从函数名和 docstring/显式描述生成工具元数据；
  - 可选注入 `ToolContext`；
  - 函数返回值统一归一化为 `ToolResult`。
- 声明式工具注册后必须完整经过现有 Registry 链路：参数校验、权限、预算、审计、observer、
  输出限制和 lifecycle。
- 未声明权限解析器时保持现有 `Tool` 的保守默认权限，不能自动放行。
- 增加单元测试，覆盖 Schema、默认值、Literal/Optional/容器类型、上下文注入、未知字段容忍、
  权限默认拒绝、异常归一化和 Registry 集成。
- 从 `assistant_agent.tools` 导出公开 API。

### 可选

- 选择一个无副作用的小型内置工具做迁移样例。只有在适配层测试稳定且迁移不降低可读性时进行。

### 不做

- 不修改 `agent/loop.py`。
- 不迁移所有现有工具。
- 不实现 CodeAgent 或执行模型生成的 Python。
- 不改变 MCPTool、Skill 或权限策略。
- 不引入新的运行时依赖；Schema 生成复用项目已有 Pydantic。

## 技术设计

`@agent_tool` 返回一个 `FunctionTool` 实例。适配器在装饰时检查函数签名并构造 Pydantic 参数
模型，再由模型生成 Draft 2020-12 兼容 JSON Schema。名为 `ctx` 且注解为 `ToolContext` 的参数
不暴露给模型，由运行时注入。

函数可返回 `ToolResult` 或字符串；字符串转换为 `ToolResult.ok`。函数异常在适配器边界转换为
稳定的 `ToolResult.error(code="tool_exception")`，不向模型泄漏异常文本或 traceback。当前
Registry 是同步执行链路，因此异步函数在装饰时快速失败。

权限解析器是可选回调。未提供时沿用 `Tool.permission_requests` 的未知扩展保守策略；提供时仅负责
声明 `PermissionRequest`，最终决策仍由 Registry 和 `PermissionPolicy` 执行。

## 测试计划

- 定向：`pytest tests/test_declarative_tools.py tests/test_tool_contract.py`
- 全量：`pytest -q`
- 质量：`ruff format --check .`、`ruff check .`、`mypy src`
- 架构：确认新增模块只依赖 `tools` 同层和 Pydantic，不反向依赖 `agent`/`ui`。

## 验收标准

- 一个带类型注解的函数可以通过装饰器生成合法工具 Schema 并注册执行。
- 所有声明式工具调用仍经过现有 Registry 安全链路。
- 未配置权限的声明式工具默认不能在安全策略下静默执行。
- 现有工具行为和测试不回退。
- 全量 pytest、Ruff、mypy 通过。

## 风险边界

- 类型系统到 JSON Schema 的映射以 Pydantic 支持范围为准；不支持的签名在装饰时快速失败。
- 不为追求简洁绕过 `ToolResult`、Registry 或权限系统。
- 第一阶段只提供增量入口；是否迁移现有工具由后续重复度和维护收益决定。

## 实施结果

- 新增 `tools/declarative.py`，提供 `@agent_tool`、`FunctionTool` 和 `PermissionResolver`。
- 类型注解经 Pydantic 生成 Draft 2020-12 Schema；默认值、Literal、Optional、容器和未知字段
  容忍均有确定性测试。
- `ctx: ToolContext` 仅运行时注入；异步函数、无注解、可变参数、位置专用参数和错误上下文签名
  快速失败。
- 未声明权限解析器时沿用未知扩展工具的保守权限；声明后仍由 Registry 决策，不能绕过权限门。
- 字符串和 `ToolResult` 正常归一；函数异常与非法返回类型转换为稳定错误结果。
- 新增 14 个测试；全量 497 passed、3 skipped，覆盖率 82%，Ruff/format/mypy 全绿。
