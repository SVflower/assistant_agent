# M7c — MCP Streamable HTTP transport

> **状态：评审已修订（v2），待稳定后细化冻结**。评审指出：① 重连不得重放已发送工具调用（幂等）；
> ② 协议头/session-id 由 SDK 代管，不自己拼；③ 规范正转向 stateless（评审引 2026-07-28 RC），
> client 握手可能变——**等稳定版 SDK v2 与规范定稿后再冻结详细方案**。本稿为方向骨架 + 硬约束。
> 里程碑正式计划（方向性，M7b 完成后开工时细化）。承接 M7b 的 MCPManager 抽象。

## 解决什么

M7b 做了 stdio（本地 server）。远程 server（托管服务、团队共享的 MCP endpoint）走 HTTP。
本期给 M7b 的 transport 抽象加一个 Streamable HTTP 实现，让同一套 MCPManager /
MCPTool / 命名空间 / 错误双通道复用，只换连接方式。

## 前提：M7b 要留好 transport 缝

M7b 实现时，连接部分应抽象成"给我一个 (read, write) 流 + session"的工厂，stdio 是其中一种。
M7c 只新增 HTTP 工厂，不动 MCPManager 的生命周期 / 同步桥 / 工具注册逻辑。
**若 M7b 没留缝，M7c 第一步是补这个抽象**（重构 MCPManager 的连接入口）。

## 范围（本期做 / 不做）

**做：**
- Streamable HTTP transport：用 SDK 的 `streamablehttp_client`，**行为**上支持远程 server 的握手/发现/调用。
- 配置扩展：`type: http` + `url` + `headers`（含 `${VAR}` 插值）。
- 基础鉴权：`headers` 里带 `Authorization: Bearer ${TOKEN}`（覆盖多数现状 server）。
- 连接可恢复（见"重连不重放"）。

**交给 SDK、不自己实现（评审）：**
- `Mcp-Session-Id` 会话头的保存/回带、404 重建、`MCP-Protocol-Version` 头——SDK 已代管。
  本期只做**配置透传**，验收看**行为**（能连、能调、断了能恢复），不要求自己拼装/保存协议头。

**不做（本期）：**
- 完整 OAuth 2.1 授权流（Resource Indicators、confused-deputy 防护）——现状 server 采用率低，信号驱动：真遇到要 OAuth 的 server 再做。
- 旧版 HTTP+SSE 双端点（deprecated）——除非遇到只支持旧版的 server 才加 fallback。
- **自动重放已发送的工具调用**（见"重连不重放"——正确性阻断项）。

## 关键设计点

**1. transport 工厂复用**
- M7b 的 MCPManager 在守护线程 loop 里，对每个 server 调"连接工厂"拿到 session。
- stdio 工厂：`stdio_client(StdioServerParameters(...))`。
- http 工厂（本期）：`streamablehttp_client(url, headers=...)`——SDK 同款 `ClientSession` 包裹，握手/tools/list/call 逻辑完全不变。
- 选哪个工厂由 `MCPServerConfig.type`（stdio/http）分派。

**2. 协议头/session — 交给 SDK**
- `Mcp-Session-Id` 保存/回带、404 重建、`MCP-Protocol-Version`：SDK 负责，Manager **不重复实现**。
- 我方只透传 url/headers，验收 SDK 的行为对不对，不碰底层协议帧。

**3. 重连不重放（正确性阻断项）**
- HTTP 比 stdio 易瞬断。断线后**可以恢复连接**（重建 session），但**默认绝不自动重放已发送的
  `tools/call`**——写文件、发消息、下订单等副作用工具重放一次就是执行两次。
- 一次工具调用途中断线 → 转 `ToolResult.error`（"调用未确认完成，请重试"），把重试权交回模型/用户，
  而不是 client 偷偷重发。
- 仅当**协议提供幂等键**、或工具被**显式声明并验证为幂等**（如只读 readOnlyHint 且我方信任该 server）
  才允许自动重试——本期默认不开。
- 超时仍复用 M7b 同步桥（fut.result(timeout) + cancel），超时转 ToolResult.error。

## 安全

- **HTTP 是网络出口**：`headers` 里的 token 经 `${VAR}` 从环境取，不落配置。
- 只连 https（http 升级 https）；本期不做 http 明文远程。
- token 是发往第三方 endpoint 的凭据——文档提示"只配你信任的 endpoint"。
- 复用 M7b 的确认/预算/审计（MCPTool 不变）。

## 文件改动清单

**新增：**
- `src/assistant_agent/mcp/transport.py`（若 M7b 未抽）：stdio/http 工厂分派。
- 或直接在 `mcp/manager.py` 加 http 分支（视 M7b 结构定）。

**修改：**
- `config/schema.py`：`MCPServerConfig` 加 `type`（默认 stdio）、`url`、`headers`。
- `mcp/manager.py`：连接入口按 type 分派工厂。
- `tests/test_mcp.py`：加 HTTP 路径测试。
- `config.example.yaml`：http server 示例（注释）。

## 测试计划

**CI 确定性单测：**
1. **fake HTTP MCP server**（本地 stub 或 mock transport）：走 SDK 完成 initialize → tools/list → call（验行为，不断言协议帧）。
2. **type 分派**：config type=http 走 http 工厂、stdio 走 stdio 工厂。
3. **headers 插值**：`${TOKEN}` 从环境正确注入请求头。
4. **重连不重放（阻断）**：工具调用途中断线 → 连接可恢复，但**已发的 tools/call 不被自动重发**；
   返回 ToolResult.error（未确认完成），副作用工具不会执行两次。
5. **超时**：请求超时转 ToolResult.error 且清理，循环不卡。
6. **回归**：M7b stdio 路径不受影响、现有测试全绿。

**真实冒烟（手动，可选）：**
- 若有可用的远程 MCP endpoint（或本地起官方 server 的 http 模式）实测；无则如实记录。

## 验收标准

1. 配一个 `type: http` server，工具与 stdio 同样以 `mcp__<server>__<tool>` 出现、可调用。
2. 连接可恢复（SDK 管 session）；**已发工具调用绝不自动重放**，副作用不重复执行。
3. headers token 从环境注入、不落配置。
4. stdio 路径回归无损；内核 loop.py 仍零改动。
5. CI 单测全绿，ruff + 架构测试通过。
6. 开工前确认 SDK 版本与规范状态（评审提 2026-07-28 RC 转 stateless）——不稳定则推迟冻结。

## 顺带（交付后）

- TECH_DEBT 登记：OAuth 未做（信号驱动）、旧版 SSE fallback 未做。
- 状态文档同步（DoD 第 6 条）。
