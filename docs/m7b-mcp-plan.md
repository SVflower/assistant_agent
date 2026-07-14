# M7b — MCP client（stdio 最小可用）

> **状态：评审已修订（v2）**。首版评审提出 3 阻断（权限确认、schema 撑爆、超时清理）+ 设计问题，
> 均已并入。开工前置：权限策略、工具过滤/上限、名称规范化、Runtime 生命周期已写入本稿。
> 里程碑正式计划。前置调研见本会话对 MCP 2025-06-18 规范、官方 python-sdk、
> Claude Code/Codex/Cursor 配置约定的调查。承接 M7a 搭好的动态注入足场。

## 解决什么

agent 的工具集是内置写死的（读写/shell/git/…）。要接入外部工具生态（浏览器自动化、
数据库、第三方 API），需要一个标准协议。MCP（Model Context Protocol）是 Anthropic 的
开放标准：外部 server 用 JSON-RPC 暴露工具，client 发现并调用。**MCP 工具 = 新的工具来源**，
接进 registry 后与内置工具同流，自动被 M6 审计 / M6.5 预算罩住。

> **评审更正**：危险确认**不是** registry 自动施加的。`registry.execute()`（registry.py:117）
> 只做预算与审计；确认由每个 `Tool.run()` 主动调 `ctx.request_confirm(category, msg)`（见
> shell.py:81、file_ops.py:82）。所以 MCP 工具**必须自己发起确认**，否则外部工具会无确认执行——
> 见下"权限策略"。

本期做 client 侧、stdio transport（本地 server 首选），HTTP 留 M7c。

## 范围（本期做 / 不做）

**做：**
- 新增 `mcp/` 层：`MCPManager`（连接生命周期 + 同步桥）、`MCPTool`（Tool 适配器）。
- stdio transport：spawn server 子进程 → initialize 握手 → `tools/list` 发现 → 注册到 registry。
- 同步桥：专用线程常驻 event loop，`MCPTool.run()` 同步调用异步 session。
- 命名空间：`mcp__<server>__<tool>` 防冲突，dispatch 时剥前缀。名称规范化 + 截断后碰撞检测。
- **权限策略（阻断项）**：MCP 工具默认 `ctx.request_confirm`，category 按 `mcp:<server>:<tool>` 细分。
- **工具过滤与上限（阻断项）**：`include_tools`/`exclude_tools`、每 server + 全局工具数上限、
  name/description/schema 长度上限——防 schema 撑爆上下文（D10）。
- 两条错误通道：协议 error（异常）vs `isError:true`（回喂模型）。
- `MCPConfig`（mcpServers 字典）；`/mcp` slash 命令列出 server 与工具；干净关闭。
- **Runtime 生命周期（还 D7）**：抽 `cli/setup.py`/Runtime，上下文管理器统管 logger + MCP 线程 +
  session + 子进程，含部分启动成功后失败的反向清理。

**不做（本期）：**
- HTTP / SSE transport（M7c）。
- MCP resources / prompts 原语（只做 tools）。
- OAuth（HTTP 才需要，随 M7c）。
- listChanged 动态刷新工具（启动发现一次）。
- server 自动安装（用户自备 npx/命令）。

## 架构：新增 mcp/ 层，依赖单向

```
main._setup ──组装──┐
                    ├─→ MCPManager.start(configs)   启动时：spawn+握手+tools/list
                    │      └─ 为每个发现的工具生成 MCPTool → registry.register
                    └─→ (退出) MCPManager.close()    关闭所有 session + 子进程
        ┌───────────────────┴───────────────────┐
   ┌────▼─────┐  MCPTool(Tool)              ┌────▼──────────┐
   │ registry  │  走 execute()（预算+审计）    │ MCPManager     │
   │（不认识MCP）│  确认由 MCPTool.run 自发起    │ 专用线程+loop   │
   └───────────┘                            │ run_coroutine_ │
                                            │ threadsafe 同步桥│
                                            └────────────────┘
```

- **层级**：`mcp/` 定为 rank 2（与 tools/skills 同级，叶子能力层）。只依赖 `tools/base`（MCPTool 继承 Tool）+ 外部 `mcp` 包。不依赖 agent/ui/cli/main。登记进 `_LAYER_RANK`。
- **依赖注入**：`mcp/` 不 import `config`——server 配置由 main 以参数传入（dataclass/dict），保持 mcp 层纯净、可对 fake server 单测。
- **registry / loop 不认识 mcp**：MCPTool 就是普通 Tool，走 `registry.execute()`。

## 关键设计点

**1. 同步/异步桥（最大的坎）**
- `mcp` SDK 是 asyncio；我们的 `Tool.run()` 是同步。每次 `asyncio.run()` 会重连、丢 session。
- 方案：`MCPManager` 起一个**守护线程**跑常驻 event loop（`loop.run_forever()`）。启动时在该 loop 里连接所有 server、跑 `initialize` + `tools/list`、持有 `ClientSession`。
- `MCPTool.run()` 同步侧调用：`fut = asyncio.run_coroutine_threadsafe(session.call_tool(name, args), loop); result = fut.result(timeout=cfg.timeout)`。
- 超时：`fut.result(timeout)` 抛 `TimeoutError` → 转 `ToolResult.error`，不卡死循环。

**2. 命名空间**
- 注册名 `mcp__<server>__<tool>`（对齐 Claude Code）。MCPTool 存 (server, raw_tool_name)，run 时用 raw 名调对应 session。
- 防跨 server 同名冲突；模型看到带前缀名，一眼知道是外部工具。

**3. 两条错误通道（务必区分）**
- 协议错误（未知工具/参数非法/server 故障）→ JSON-RPC error / SDK 抛异常 → 捕获转 `ToolResult.error`（我方问题）。
- 工具执行错误 → 正常响应带 `isError:true`，内容是错误文本 → 转 `ToolResult`（is_error=True，output=文本）**回喂模型**，让它据此换做法。

**4. 权限策略（阻断项，评审后新增）**
- `registry.execute` 不做确认——MCPTool.run() 必须**主动**调 `ctx.request_confirm(category, msg)`。
- **category 按 `mcp:<server>:<tool>` 细分**：`always_allowed` 是按 category 记忆的（base.py:99），
  若 MCP 共用一个 category，用户点一次"永久允许"就放行所有 server 的所有 MCP 工具——必须每工具独立。
- `annotations.destructiveHint`/`readOnlyHint` 是**不可信提示**：只能**升高**风险（触发确认），
  绝不能据此**免除**确认。默认所有 MCP 工具都确认。
- 可选 `auto_approve: false`（默认）：仅当用户显式为某 server/工具配 true 才跳过确认。

**5. 工具过滤与 schema 上限（阻断项，评审后新增）**
- MCP server 可能暴露几十上百工具，schema 全量注册会撑爆上下文（当前 context 不计 schema，D10）。
- 硬防护（不依赖 M8a 的精确 token 口径）：
  - `include_tools`/`exclude_tools`（按 server 白/黑名单）。
  - 每 server 工具数上限 + 全局 MCP 工具数上限（超出截断并 `log` 丢弃了哪些，不静默）。
  - name/description/inputSchema 长度上限，超长截断。
- 名称规范化（非法字符→合规）+ 截断后碰撞检测（撞名加序号或拒绝，绝不静默覆盖）。
- 与 M8a 关系：M8a 补"schema 计入 token 预算"的精确口径；本期先用数量/长度硬上限兜底，两者解耦。

**6. content 块提取**
- `tools/call` 返回 content 列表（text/image/resource…）。本期只提取 text 块拼成 output；非 text 块给占位说明（如"[图片省略]"）。structuredContent 若有可附加。

**7. 启动失败隔离 + Runtime 生命周期（还 D7，评审后强化）**
- 某个 server 连不上/握手失败 → log 警告、跳过，不影响其他 server 与内置工具。绝不因一个坏 server 让 agent 起不来。
- **直接还 D7**（不再"膨胀后再拆"）：MCP 引入启动/状态/关闭/异常清理，main.py（已 329 行）会进一步膨胀。
  M7b 第一步先抽 `cli/setup.py` 或 `Runtime` 组装对象，用**上下文管理器**统管 logger + MCP 线程 +
  session + 子进程。
- **反向清理**：前 N 个 server 起好、第 N+1 个失败时，要把已起的干净关掉（或保留并明确记录），不留半开状态。
- **超时不只是返回**：`fut.result(timeout)` 抛超时后，还要 `fut.cancel()` 并在 loop 侧等待协程清理，
  防止 server 端悬挂请求/僵尸子进程。

**8. 依赖**
- 新增 `mcp` 包（官方 python-sdk），**固定经过验证的稳定版**（pin），隔离一层 transport adapter
  （为 M7c 换 HTTP 工厂留缝）。放 `pyproject.toml` 主依赖（MCP 是核心能力，非可选）。

## 配置（对齐 Claude Code mcpServers 约定）

```yaml
mcp:
  max_total_tools: 60        # 全局 MCP 工具数上限（防 schema 撑爆上下文）
  servers:
    playwright:
      command: npx
      args: ["-y", "@playwright/mcp@latest"]
      env: {}
      enabled: true
      timeout: 30
      auto_approve: false    # 默认所有工具都需确认
      max_tools: 40          # 该 server 工具数上限
      include_tools: []      # 空=全部（再受上限约束）
      exclude_tools: []
```

- 环境变量插值 `${VAR}`：`env`/后续 headers 里的 `${TOKEN}` 从进程环境取，密钥不落配置文件。
- `enabled: false` 跳过该 server。
- `auto_approve`/`include_tools`/`exclude_tools`/`max_tools`/`max_total_tools` 见"权限策略"与"工具过滤"。

## 安全

- **外部 server 是信任边界外**：env 只传该 server 需要的最小变量，绝不转发整个进程环境。
- MCP 工具**主动**发起确认（MCPTool.run 调 request_confirm，category `mcp:<server>:<tool>`）+ 预算 + 审计。`destructiveHint`/`readOnlyHint` 是**不可信提示**：只升高风险、绝不免除确认。
- server 命令来自用户配置文件——文档提示"只配你信任的 server，等同装依赖"。
- 子进程 stdout 只收 MCP 消息，stderr 可弃/记日志；防 server 往 stdout 打脏数据破坏协议。

## 文件改动清单

**新增：**
- `src/assistant_agent/mcp/__init__.py`
- `src/assistant_agent/mcp/manager.py`：`MCPManager`（线程+loop、连接、发现、过滤/上限、关闭）。
- `src/assistant_agent/mcp/tool.py`：`MCPTool(Tool)`（命名空间、确认、同步桥调用、错误双通道、content 提取）。
- `src/assistant_agent/cli/setup.py`（还 D7）：`Runtime` 组装对象/上下文管理器，统管 logger + MCP + session + 子进程生命周期。
- `tests/test_mcp.py`：fake server 单测。

**修改：**
- `config/schema.py`：`MCPConfig`（max_total_tools）+ `MCPServerConfig`（含 auto_approve/max_tools/include/exclude），挂到 `AppConfig`。
- `main.py`：把 wiring 迁到 `cli/setup.py`；命令函数只调 Runtime（main 瘦身，还 D7）。
- `cli/commands.py`：`/mcp` 命令（列 server 状态与工具）。ChatContext 加 mcp 信息。
- `tests/test_architecture.py`：`_LAYER_RANK` 加 `mcp: 2`。
- `config.example.yaml`：mcp 段示例（注释默认关，避免新手没装 npx 报错）。
- `pyproject.toml`：加 `mcp` 依赖（pin 稳定版）。

## 测试计划（两层，见记忆 m7b-mcp-test-strategy）

**第一层 · CI 确定性单测（不依赖网络/Node）：**
1. **进程内 fake MCP server**（或录制 JSON-RPC 报文）：initialize 握手、`tools/list` → schema 适配正确（name/description/inputSchema→parameters）。
2. **tools/call 正常**：text content 提取、多 content 块拼接。
3. **两条错误通道**：协议 error → ToolResult.error；`isError:true` → is_error=True 且文本回喂。
4. **命名空间**：`mcp__srv__tool` 注册名；两 server 同名工具不冲突；dispatch 剥前缀调对名。
5. **超时**：慢工具 → `fut.result(timeout)` 抛 → ToolResult.error，循环不卡。
6. **权限确认（阻断）**：MCPTool.run 触发 request_confirm；category 为 `mcp:<server>:<tool>`；
   对 server A 点"永久允许"不放行 server B 或同 server 其他工具；destructiveHint 只升不免。
7. **工具过滤/上限（阻断）**：include/exclude 生效；超 max_tools/max_total_tools 截断并 log 丢弃项；
   超长 name/description/schema 截断；截断后撞名有碰撞处理，不静默覆盖。
8. **启动失败隔离 + 反向清理**：坏 server 跳过、其余正常；部分起好后失败能反向关掉已起的。
9. **关闭 + 超时清理**：close 后子进程/session 干净回收；超时后 fut.cancel() 且协程被清理，无僵尸。
10. **禁用**：`enabled=false` server 不 spawn、不注册工具。
11. **架构**：mcp 层不依赖 agent/ui；`_LAYER_RANK` 含 mcp。
12. **回归**：现有测试全绿；main 瘦身后 run/chat 行为不变。

**第二层 · 真实 stdio 冒烟（手动/本地，不进 CI）：**
- 配 `npx @playwright/mcp@latest`，实测：spawn → 握手 → 发现工具（browser_navigate 等）→ 调一个真实工具 → 拿结果 → 干净关闭。
- 外部环境阻塞（无 Node/下载失败）就如实记录，同 M6.5 LM Studio 处理。

## 验收标准

1. 配一个 stdio server，启动后其工具以 `mcp__<server>__<tool>` 出现在 registry，模型可调用。
2. **每个 MCP 工具调用触发确认**（除非 auto_approve）；"永久允许"按 server+tool 粒度，不跨工具放行。
3. **工具过滤/上限生效**：include/exclude、每 server + 全局上限、长度上限、碰撞处理都工作。
4. 工具执行错误（isError）回喂模型；协议错误转 ToolResult.error 不崩循环。
5. 坏 server 不影响其他 server 与内置工具启动；部分启动失败能反向清理。
6. 超时不卡死且清理协程/子进程；进程退出时 session/子进程干净关闭。
7. `enabled=false` 零副作用（不 spawn）。
8. `/mcp` 列出 server 与其工具。
9. **内核 loop.py 零改动**（MCPTool 是普通 Tool，走 registry）；main 瘦身、D7 还清。
10. 第一层单测全绿，ruff + 架构测试通过；第二层 Playwright 冒烟实测或如实记录阻塞。

## 顺带（交付后）

- D7 在本期还清（抽 `cli/setup.py`/Runtime）；若仍超软线，登记残留。
- TECH_DEBT 复盘登记新债（如：resources/prompts 未做、listChanged 未做、schema token 口径归 M8a）。
- 状态文档同步（DoD 第 6 条）：ROADMAP/CLAUDE/AGENTS/README，数字实测。
