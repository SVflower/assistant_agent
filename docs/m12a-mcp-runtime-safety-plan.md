# M12a 通用 MCP 运行时安全方案

> 状态：已完成（2026-07-16）。
> 边界：仅增强 Agent 的通用 MCP client，不包含任何业务 MCP、业务接口或领域模型。

## 目标

让任意符合 MCP 协议的外部 server 都能在不修改 Agent 业务代码的前提下接入，并为只读、写入、
恢复和传输异常提供一致且可配置的安全语义。

## 范围

### 必做

- 通过 MCP request `_meta` 透传稳定的 `trace_id/session_id/run_id/call_id`。
- 读取 tool annotations，但只有显式信任的 server 才能用 `readOnlyHint` 降低风险。
- 支持 per-tool replay、timeout 和 transport-error outcome 策略。
- 写调用超时或断线时返回 `unknown`，禁止自动重放。
- server 声明 `outputSchema` 时校验 `structuredContent`。
- MCP 配置允许非敏感环境字面量，疑似密钥字段仍强制使用环境变量引用。
- 所有 MCP 工具继续经过统一权限、审计、预算和恢复链路。

### 不做

- 不内置任何业务 MCP、URL、DTO、工具名或领域判断。
- 不根据工具名称猜测只读或写入语义。
- 不允许 annotations 或 tool policy 绕过权限系统的 `deny/ask`。
- 不修改 `agent/loop.py`，不进行全栈 async 重构。

## 通用配置

```yaml
mcp:
  servers:
    records:
      type: stdio
      command: records-mcp
      trust_tool_annotations: true
      tool_policies:
        read_record:
          replay: safe_readonly
          timeout: 15
        update_record:
          replay: requires_decision
          outcome_on_transport_error: unknown
          timeout: 60
```

`tool_policies` 只描述执行与恢复语义；权限是否允许、询问或拒绝仍由统一
`permissions.rules` 决定。

## 测试计划

- 关联 ID 透传且模型参数不能覆盖。
- 未受信只读声明不能降低恢复风险。
- destructive annotation 优先于错误的只读策略。
- 可信只读工具传输失败可重试，写工具失败进入 unknown 且不重放。
- output schema 不匹配返回稳定 contract error。
- per-tool timeout、默认 fail-closed 与配置解析有确定性测试。
- stdio/HTTP 现有连接、发现、过滤、命名空间和关闭测试不回退。

## 验收

1. 新 MCP 只需配置 server，不需要修改 Agent 源码。
2. 第三方 server 默认不因自报只读而获得更低权限或自动恢复。
3. 写调用结果未知时不自动重发，并保留调用关联信息供外部核对。
4. 结构化输出不符合声明契约时不会继续作为成功结果使用。
5. Agent pytest、覆盖率、Ruff、mypy 全绿，且未修改内核 Loop。
