# M20 Agent -> API 交接

> 日期：2026-07-19
> Agent 基线 commit：`d490e08bb6cab8c8a20c2b4edbe557760f6829b2`
> M20 implementation：本提交
> 公共事件契约：`EVENT_CONTRACT_VERSION == 1`（不提升）
> Run checkpoint：schema v3（不变）

## 1. 给 assistant_agent_api AI 的执行说明

请先完整阅读 Agent 安装版本中的
`docs/agent-service-integration-guide.md`，然后按本文件检查 API。不要修改 Agent 仓库，不要复制
MCP 生命周期、Session/Run 状态机或终端渲染逻辑。

M20 将 optional MCP 从 Runtime 创建关键路径移除，并增加安全启动阶段与动态 MCP capability 状态。
StepEvent、Interaction、Run terminal、checkpoint 和恢复语义没有变化。

## 2. API 必改项

1. 更新并固定包含 M20 的 Agent wheel/commit；启动时继续断言 `EVENT_CONTRACT_VERSION == 1`。
2. capability DTO/枚举接受新增 MCP 状态：
   `discovering`、`available_cached`、`restart_required`、`connecting`；未知未来值按 unavailable 展示，
   不导致反序列化崩溃。
3. 每次读取 Session capability 时使用 `session.capabilities` 的当前值，不永久缓存 Runtime 创建时的
   首次快照。
4. 服务 readiness 不等待 optional MCP。只有 Runtime 配置、Workspace、Provider 装配或 required MCP
   创建失败才使对应 Runtime 不可用。
5. `available_cached` 只表示当前 Runtime 有稳定 Tool Schema，不能展示为“已连接”；第一次实际调用
   可能进入 `connecting`，随后为 `connected` 或 `degraded_*`。
6. `discovering` 表示当前 Runtime 没有该 server 的工具；变为 `restart_required` 后不得向活跃 Runtime
   热插拔。仅在 Session 无 active/paused/unfinished Run 时按既有淘汰策略重建 Runtime。
7. Runtime 关闭或 Session 淘汰继续调用公共 `close()`；不要自己管理目录发现线程或 MCP 子进程。

## 3. 可选接入项

低层 `create_runtime` 新增可选参数：

```python
startup_observer: Callable[[RuntimeStartupEvent], None] | None
```

`RuntimeStartupEvent` 只从 `assistant_agent.contracts` 导入：

```python
from assistant_agent.contracts import RuntimeStartupEvent
```

当前高层 `AgentService.create_session/load_session` 不接收 observer。API 如果不展示 Session Runtime 创建
进度，无需改造。若确有展示需求，应在单独设计后扩展公共高层工厂，不能让 API 穿透 bootstrap 或复制
Session/Run 状态机。

启动事件字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `phase` | string enum | 安全启动阶段 |
| `status` | `started \| completed \| failed` | 阶段状态；failed 不携带原始异常 |
| `message` | string | 可展示安全文本，不含配置、路径和原始异常 |

合法 phase：`loading_config`、`starting_workspace`、`discovering_skills`、`starting_web`、
`preparing_mcp`、`creating_loop`、`ready`。

该事件不是 StepEvent，不进入 Run WebSocket；API 若转发，应使用独立的 Session/Runtime 创建进度事件，
自行增加 seq/timestamp。observer 异常不会中断 Runtime 创建。

## 4. MCP 状态映射

| Agent 状态 | API 建议状态 | online | 说明 |
|---|---|:---:|---|
| `available_cached` | available_on_demand | false | Schema 已注册，调用时连接 |
| `discovering` | discovering | false | 后台发现，当前 Runtime 无工具 |
| `restart_required` | restart_required | false | 下一 Runtime 生效 |
| `connecting` | connecting | false | 首次工具调用正在连接 |
| `connected` | connected | true | 当前 Runtime 已连接 |
| `degraded_timeout` | unavailable | false | 超时降级 |
| `degraded_connection` | unavailable | false | 连接失败 |
| `degraded_discovery` | unavailable | false | 工具发现失败 |
| `required_failed` | unavailable_required | false | required 创建失败 |
| `blocked_by_policy` | blocked | false | 部署 policy 禁止 |
| `disabled` | disabled | false | 配置关闭 |

不要根据中文 `message`、notice 或日志推导状态。

## 5. 完整场景

### 5.1 optional MCP 有有效目录

```text
create/load Session
  -> capabilities: available_cached + tool_names
start Run
  -> StepEvent 流保持 v1
首次调用 MCP tool
  -> capabilities: connecting
  -> connected 或 degraded_*
run_terminal
```

### 5.2 optional MCP 无目录

```text
create/load Session 立即完成
  -> capabilities: discovering，tool_names 为空
后台发现成功
  -> capabilities: restart_required + discovered tool_names
当前 Runtime 工具不变化
空闲且无未完成 Run 时重建 Runtime
  -> capabilities: available_cached
```

### 5.3 required MCP 失败

```text
create/load Session
  -> RuntimeDependencyError
  -> API 使用既有 503 capability_unavailable 映射
  -> 已创建资源由 Agent 回滚
```

## 6. 联调验收

1. optional MCP 离线时 API 仍 ready，内置工具和普通对话可用。
2. required MCP 失败时对应 Session Runtime 创建失败并返回类型化错误。
3. `available_cached` 不显示为在线，首次工具调用后状态动态变化。
4. `discovering -> restart_required` 不改变活跃 Runtime 的工具列表。
5. API 能容忍未来未知 MCP 状态，并按 unavailable 显示。
6. Session 关闭后无后台发现线程、MCP 子进程或 transport 遗留。
7. StepEvent v1、`final -> run_terminal`、pause/cancel/resume 和 Interaction 测试无需改语义且继续全绿。

## 7. 兼容性结论

- **破坏性变化：无。**
- **公共 additive 变化：有。** 新增 `RuntimeStartupEvent` 和 MCP 状态值。
- **契约版本：不提升。** 新类型不进入 StepEvent，既有 DTO 字段未删除或改义。
- **API 最小必要修改：** 放宽并正确映射 MCP 状态，动态读取 capability，readiness 忽略 optional MCP。
- **Web 修改：** 仅在产品要展示这些状态时需要；不得把工具目录状态展示成在线状态。
