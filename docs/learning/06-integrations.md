# 06 Provider、Skill、MCP 与 Web

## Provider：模型适配器

核心只依赖 `providers/ports.py` 定义的消息、工具调用和流式 chunk。`providers/litellm.py:LLMClient`
负责：

1. 把统一配置转换成 LiteLLM 请求。
2. 解析不同 provider 的流式碎片。
3. 累积文本、reasoning、tool call arguments 和 usage。
4. 把 429、503、timeout 等第三方异常映射成稳定失败分类。

因此切换 DeepSeek、OpenAI 兼容服务或本地 LM Studio 只改配置。业务代码不能判断某个 provider 名称。

流式 tool arguments 可能被拆成多个字符串碎片，adapter 必须按 call index/ID 合并后再交给 Agent。小模型
偶尔输出不规范 JSON，容错和错误反馈也应留在边界层。

## Skill：按需加载的知识

Skill 是一份 `SKILL.md` 操作说明，不是常驻 Python 插件。启动时只发现并注入受信 Skill 的有界元数据；
模型判断相关时调用 `load_skill` 读取正文。这叫渐进披露，可以节省上下文。

来源和信任很重要：个人 Skill、项目 Skill、自定义目录有不同默认信任。未显式信任的项目 Skill 不进入
模型上下文。Skill 可以指导模型使用现有工具，但不能绕过工具权限。

## MCP：外置工具协议

MCP server 是独立进程或远程服务。Agent 仓库只保留通用 client，不放 Maxwell 等业务代码。

MCP SDK 使用 async，而 Agent Loop 是同步的。`MCPManager` 的做法是：

```text
同步 Runtime
  -> 后台线程中的 asyncio event loop
  -> MCP session / transport
  -> future.result(timeout) 把结果交回同步工具
```

每个 MCP 工具仍包装成普通 Tool，因此继续经过 Registry 的参数校验、权限、预算、审计和恢复策略。

`startup: optional` 的 server 不在启动关键路径：目录可先注册，后台发现或首次调用时惰性连接；失败只
形成 degraded capability。`required` 则表示该依赖缺失时 Runtime 创建失败。发送后的写调用不能因为
断线自动重放。

## Web：联网工具与 SSRF 边界

`web_search` 返回结构化搜索结果；`fetch_url` 读取公开网页。安全层拒绝 URL 凭据、localhost、私网、
链路本地、metadata 地址和危险重定向，并限制 DNS 解析、响应大小、超时和正文长度。

Web deployment profile 目前只对已证明安全的能力自动放行。不能因为工具叫“读取”就默认安全：通用
fetch 如果不能证明 DNS rebinding 与重定向目标安全，就不应进入免审批白名单。

## Chart Artifact

`present_chart` 不接受任意 ECharts option，而是验证版本化 `ChartSpecV1`/`ChartSpecV2`，只允许受控图表类型和数据。
它拒绝 JS formatter、HTML、外部 URL 和任意执行代码。成功后生成不可变 Artifact，ItemEvent 通过
结构化 `chart` 字段交给 UI；失败不改变文字回答和 Run terminal。

## 什么时候选哪种扩展

| 需求 | 选择 |
|---|---|
| 调整模型接入格式 | Provider adapter |
| 教模型一套流程或领域知识 | Skill |
| 调用独立业务/API/浏览器服务 | 外置 MCP |
| 与 Agent 状态紧耦合的通用安全能力 | 内置 Tool |
| 只改变展示 | CLI/Web adapter，消费 ItemEvent |

不要为了一个业务 API 修改 Agent Loop，也不要让 Skill 直接获得 Python 执行特权。

