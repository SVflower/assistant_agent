# M17 CLI/Web 双入口与生产 Runtime 策略方案

> 状态：已完成并通过验收。
>
> 日期：2026-07-18。
>
> 内核结论：**不需要修改 `src/assistant_agent/agent/loop.py`**。本期只调整公共 Runtime 装配、
> 扩展发现策略、MCP 启动行为和服务能力快照。若实现中发现必须改变 Loop 状态机，将停止并另行申请
> 授权。

## 1. 问题定义

CLI 和 Web 的用户交互与进程生命周期确实不同：

| 维度 | CLI | Web/API 生产服务 |
|---|---|---|
| 使用边界 | 单机当前用户 | 单一部署所有者，通过 Web 使用多个 Session |
| 生命周期 | 启动命令时创建 Runtime，退出即关闭 | API 长期运行，Session Runtime 按需创建和淘汰 |
| 交互 | Console 同线程直接询问 | 工作线程阻塞，HTTP/WebSocket 从另一线程响应 |
| Workspace | 当前目录通常就是用户项目 | 使用部署时固定的服务 workspace 根目录 |
| Skill | 可使用个人和项目 Skill | 默认使用服务配置允许的、版本固定的 Skill |
| MCP | 本机子进程或个人远程服务 | 使用服务端固定配置，连接数量和失败降级必须受控 |
| 扩展安装 | 用户可在本机授权安装 | Web 首期默认关闭，避免请求修改部署配置 |
| 启动体验 | 可以短暂等待扩展连接并展示 banner | API 进程不能等待所有 MCP；单 Session 创建也必须有界 |
| 事件 | 直接渲染 StepEvent | 转换 DTO、编号、缓存、广播和断线重连 |

但这些差异不能演变成两套 Agent：

```text
CLI Adapter ─┐
             ├─ Runtime Factory -> SessionRuntime -> Run -> StepEvent
Web Adapter ─┘
```

`AgentLoop`、Tools、权限、checkpoint、恢复和 Session 同步必须继续共用。不同入口只负责策略、调度、
交互和展示。

## 2. 当前能力评估

### 2.1 M16 已解决

- UI 无关 `create_runtime()` 和 `AgentService`；
- Console 不再是 Runtime 必选依赖；
- 同步 `InteractionPort` 可跨线程授权、澄清和恢复；
- 一个 SessionRuntime 对应隔离 Conversation、RunControl、权限记忆和 MCPManager；
- 同 Session 单 Run、不同 Session 可分线程运行；
- `StepEvent v1`、敏感 reasoning 和无歧义 `run_terminal`；
- 固定 `config_path/workspace_root`，不依赖并发修改 `os.chdir()`；
- CLI 已复用公共工厂和 Session/Run 门面。

因此不需要为 Web 复制一套 Loop，也不需要把 Agent 全栈改成 async。

### 2.2 仍需 Agent 改造的缺口

| 缺口 | 当前行为 | 生产风险 |
|---|---|---|
| 缺少不可绕过的 Runtime 策略 | 能否管理 Skill/MCP 主要依赖权限规则和配置 | Web 请求可能意外修改部署使用的扩展配置 |
| Skill 默认来源含服务器 OS 用户目录 | `skills.dirs` 为空时发现 personal/project/legacy | 服务进程可能加载部署者未计划提供给 Web 的个人 Skill |
| MCP 启动串行 | 每 Runtime 按 server 顺序连接 | N 个失败 server 的等待接近 timeout 总和 |
| 连接与调用共用 `timeout` | 同一字段控制 initialize/list 和 call_tool | 为长调用设置大 timeout 会拖慢 Session 启动 |
| MCP 无 required/optional 语义 | 单 server 连接失败统一 warning + skip | 无法表达“可选搜索”和“本任务必需 MES”的区别 |
| 缺少结构化能力快照 | 只有启动 notices 和 MCP warning 文本 | API/前端难以稳定展示 connected/degraded/disabled |
| 失败 MCP 只在新 Runtime 重试 | 当前 Runtime 工具 Schema 在启动时冻结 | 长期 Session 不知道能力已经恢复 |
| stdio MCP 每 Runtime 一份 | 每个 SessionRuntime 创建 MCPManager | 大量 Session 可能产生大量子进程和连接 |

### 2.3 应留在 API 仓库的职责

以下不是 Agent 改造项：

- 服务监听、基础访问令牌和网络边界；
- 固定 config/workspace 的部署配置；
- Runtime Pool、线程池、并发配额和空闲淘汰；
- REST/WebSocket、事件 seq、时间戳、heartbeat、缓存与重连；
- Web DTO 和前端能力状态页面；
- 数据库、Redis、多实例租约；
- 业务任务需要哪些 MCP 的产品级 capability profile；
- 未来的账号、注册、用户存储、租户、RBAC 和 Session 所有权模型。

Agent 只提供可被这些控制面安全调用的进程内边界。

## 3. 目标架构

### 3.1 CLI 路径

```text
Typer 参数
  -> 查找本机 config + 当前 workspace
  -> CLI RuntimePolicy
  -> ConsoleInteractionAdapter
  -> AgentService/SessionRuntime（可接受有限启动等待）
  -> StepEvent -> Console renderer
```

CLI 保留：

- 本机 personal/project Skill；
- 经权限确认后的 Skill/MCP 自助管理；
- 本机允许的 stdio MCP；
- banner、启动 warning 和直接终端交互。

### 3.2 Web 路径

```text
API lifespan
  -> 只加载服务配置和 AgentServiceRegistry，不创建所有 Runtime

Session 请求
  -> 使用部署时固定的 config + workspace
  -> Service RuntimePolicy
  -> 工作线程中 lazy create/load SessionRuntime
  -> StepEvent -> EventHub -> WebSocket
  -> InteractionRequest -> Broker -> REST response -> InteractionPort
```

Web 默认：

- 禁止模型调用扩展安装/卸载工具；
- 不读取服务器 OS 用户的 personal Skill；
- MCP 只来自服务端允许的配置；
- 不允许请求传入任意 config_path、workspace_root、stdio command 或 MCP URL；
- 可选 MCP 失败时 Session 降级启动；
- 必需 MCP 失败只阻断该 Session/能力配置，不阻断 API 进程；
- 正在运行或暂停的 Run 不热插拔工具 Schema。

## 4. M17a：不可绕过的 RuntimePolicy

### 4.1 公共策略对象

新增 UI 无关、服务端注入的冻结策略，例如：

```python
@dataclass(frozen=True)
class RuntimePolicy:
    allow_extension_management: bool = True
    allow_personal_skills: bool = True
    allowed_mcp_transports: frozenset[str] = frozenset({"stdio", "http"})
    minimum_sandbox: Literal["off", "workspace", "container"] = "off"
```

策略不是用户配置的另一份副本，而是调用方给 Runtime 的上限：

- config 可以进一步收紧，不能突破 RuntimePolicy；
- CLI 使用兼容策略，保持现有行为；
- Web 使用生产策略，例如禁止扩展管理、personal Skill 和 stdio MCP，并要求至少 container；
- 策略违反时抛类型化配置/策略错误，不能静默放宽。

`AgentService` 和 `create_runtime()` 增加可选 `runtime_policy`。为保持现有 CLI 和第三方调用兼容，缺省
使用当前行为；Web API 应显式传入服务策略。

### 4.2 扩展管理

`allow_extension_management=False` 时：

- 不向 Registry 注册 `manage_skill` 和 `configure_mcp_server`；
- 不仅依赖 `extension.manage` 权限拒绝；
- 模型完全看不到这两个工具 Schema，避免无意义调用和共享配置写入竞态；
- CLI slash 管理命令仍属于 CLI 控制面，不由公共 RuntimePolicy 自动开放给 Web。

### 4.3 Skill 来源

`allow_personal_skills=False` 时：

- 默认发现不包含服务器 OS 用户目录；
- 只发现服务端配置目录和当前 workspace 中被显式信任的 Skill；
- 未信任 Skill 继续只产生 notice，不注入模型；
- Skill 目录、名称、来源和版本进入能力快照，但不返回完整 Skill 内容。

### 4.4 Sandbox 下限

`minimum_sandbox` 用于防止生产配置意外设置得更弱：

```text
off < workspace < container
```

需要明确：workspace 是路径边界，不是 OS 安全沙箱。面向互不信任的公网用户时，API 应使用 container
或更强的外部隔离；RuntimePolicy 只负责拒绝错误部署，不替代容器平台和宿主机安全。

## 5. M17b：MCP 启动韧性

### 5.1 配置语义

建议增加：

```yaml
mcp:
  connect_parallelism: 4
  servers:
    search:
      enabled: true
      startup: optional
      connect_timeout: 5
      timeout: 30

    production_mes:
      enabled: true
      startup: required
      connect_timeout: 5
      timeout: 60
```

- `enabled=false`：不连接、不注册；
- `startup=optional`：连接/发现失败后记录 degraded，Runtime 继续；
- `startup=required`：失败后该 SessionRuntime 创建失败，抛类型化 dependency error；
- `connect_timeout`：只约束 connect/initialize/tools/list；
- `timeout`：只约束实际 `call_tool`；
- 旧配置没有新字段时保持 `optional` 和当前 timeout 兼容行为。

`required` 只影响正在创建的 Runtime，不应使 FastAPI 进程的 liveness 失败。API 是否选择包含 required
MCP 的配置，应由服务端 capability profile 决定。

### 5.2 并行连接

MCPManager 在已有专用 event loop 内并行连接 server，并用 semaphore 限制并发：

1. 为每个 enabled server 建立独立连接任务；
2. 每个任务使用自己的 `connect_timeout`；
3. 单个 optional 失败立即关闭其 partial stack；
4. 等待所有连接任务归并结果；
5. required 失败时关闭本次已连接的所有 server，再抛类型化异常；
6. 工具注册按配置顺序稳定排序，不能因并发完成顺序改变 Schema/hash；
7. 保持 max_tools、max_total_tools、include/exclude 和名称冲突规则。

这样启动上界接近最慢一批连接，而不是所有 timeout 相加。

### 5.3 失败分类

结构化状态只返回安全分类，不把 URL token、header、env 或完整异常发给调用方：

```text
disabled
connecting
connected
degraded_timeout
degraded_connection
degraded_discovery
blocked_by_policy
required_failed
```

详细异常仍只进入脱敏内部日志。

## 6. M17c：能力快照和安全刷新

### 6.1 公共能力快照

`AgentRuntime` 提供只读快照，例如：

```python
RuntimeCapabilities(
    sandbox="container",
    builtin_tools=(...),
    skills=(SkillCapability(...),),
    mcp_servers=(MCPServerCapability(...),),
    extension_management=False,
)
```

MCP server 快照至少包含：

- server 名；
- required/optional；
- transport；
- 状态码；
- 已注册工具数量和脱敏名称；
- 最近一次启动探测时间；
- 安全错误分类。

不得包含 headers、env、command 完整参数、密钥、工具原始 Schema 或隐藏 reasoning。

### 6.2 不在活跃 Runtime 热插拔

本期明确不实现动态修改正在运行 Runtime 的 Registry：

- AgentLoop 的工具 Schema 和恢复 definition hash 在 Runtime 创建时冻结；
- 热插拔会改变模型可见工具和 paused Run 恢复语义；
- MCP 恢复在线后，API 只标记该 Session“可刷新”；
- Session 无 active/paused Run 时，API 关闭旧 Runtime，再用 `load_session()` 创建新 Runtime；
- paused Run 必须继续由已有 definition-change 交互确认后恢复；
- 刷新会清空内存中的会话授权，属于安全收紧。

### 6.3 探测与重试所有权

Agent 提供一次性、无资源泄漏的依赖探测入口；API 负责：

- 指数退避和 jitter；
- 调度周期；
- 全局探测并发限制；
- 前端状态更新；
- 选择何时淘汰并重建空闲 Runtime。

Agent 不创建常驻重试调度线程，避免 CLI 和 Web 获得不同的隐藏生命周期。

## 7. 单一部署所有者模型

### 7.1 首期边界

当前 API 没有账号注册、用户数据库和租户模型，M17 按以下前提设计：

```text
一个 API 部署
  -> 一份服务端 config
  -> 一个固定 workspace_root
  -> 一个 Service RuntimePolicy
  -> 多个彼此隔离的 SessionRuntime
```

首期不在 Agent 或 API DTO 中增加 `user_id/tenant_id/role/owner_id` 等占位字段，也不建立空壳 RBAC。
Session 隔离仍然有效，但所有 Session 都属于同一部署所有者，并共享部署指定的文件 workspace。

### 7.2 固定 Workspace

- `workspace_root` 在 API 启动配置中确定；
- HTTP 请求不能覆盖 config_path 或 workspace_root；
- Session/Run/log/artifact 继续按该 workspace 的状态命名空间保存；
- 多个 Session 的 Conversation、RunControl、权限记忆和 MCPManager 仍各自隔离；
- 文件内容属于同一部署 workspace，不宣称用户级文件隔离。

未来加入账号系统时，再在 API 仓库增加 `principal -> workspace/profile -> Session ownership` 映射。
Agent 已支持为不同 workspace_root 创建不同 AgentService，因此本期不需要提前修改 Session 数据格式。

### 7.3 固定 MCP 身份

首期 MCP 使用部署者配置的服务凭据：

- 配置和 Secret 由服务器管理员提供；
- HTTP 请求不能提交任意 MCP URL、header、env、stdio command 或 config path；
- 不支持个人 OAuth、用户自带 MCP 或租户动态 SecretResolver；
- Web 是否允许扩展管理由 Service RuntimePolicy 决定，默认关闭；
- 每个 SessionRuntime 仍独立创建 MCPManager，避免 server session 状态和取消相互污染。

### 7.4 服务级资源配额

API 只需先实现部署级限制：

- 全局活跃 SessionRuntime 数；
- 全局并发 Run 数；
- ThreadPool worker 数；
- 每 Runtime MCP server/tool 数；
- stdio MCP 子进程总数；
- 空闲 Runtime TTL；
- 事件缓存、WebSocket 订阅者和 Interaction 等待总数。

暂不实现每用户配额或跨 Session MCP 连接池。连接共享会引入凭据、server session 状态和取消相互
影响；在真实资源数据证明必要前，优先使用 Runtime Pool 限制总量。

## 8. 健康与启动语义

生产服务应拆分：

```text
/health/live
  API 进程和事件循环存活

/health/ready
  服务配置、状态目录和 AgentServiceRegistry 可用
  不要求 optional MCP 在线

/capabilities 或 Session 创建响应
  返回该 Runtime 的 Tool/Skill/MCP/sandbox 快照
```

API lifespan 只创建轻量 AgentService/registry，不创建所有 SessionRuntime。MCP 连接发生在 SessionRuntime
lazy create/load 的工作线程中，不能阻塞 ASGI event loop。

错误建议：

| 场景 | API 行为 |
|---|---|
| optional MCP 失败 | Session 创建成功，返回 degraded capability + notice |
| required MCP 失败 | 当前 Session/profile 创建失败，503 capability_unavailable |
| 非法 MCP 配置 | 部署/配置错误，Runtime 创建失败 |
| Runtime Pool 满 | 429/503 resource_capacity |
| 同 Session 已有 Run | 409 session_busy |

## 9. 分期与范围

### M17a 必做：生产策略边界

- RuntimePolicy 公共类型；
- AgentService/create_runtime 注入；
- 禁止扩展管理工具注册；
- personal Skill 来源控制；
- MCP transport 控制；
- sandbox 下限验证；
- CLI 默认行为回归。

### M17b 必做：MCP 有界降级启动

- required/optional；
- connect_timeout 与 call timeout 分离；
- 有界并行连接和稳定工具顺序；
- 类型化 required dependency error；
- partial resource 逆序清理。

### M17c 必做：能力快照

- Tool/Skill/MCP/sandbox 结构化快照；
- 脱敏和公共导出；
- 一次性依赖探测；
- 文档明确 idle rebuild，不做热插拔。

### API 仓库随后实施

- AgentServiceRegistry 和 Runtime Pool；
- 有界 Run worker；
- EventHub/WebSocket；
- Interaction Broker；
- capability 状态和空闲 Runtime 刷新。

### 本期不做

- FastAPI/WebSocket；
- 账号、注册、用户存储、租户、RBAC 和 Session 所有权；
- 动态热注册 MCP 工具；
- 跨 Session MCP 连接共享；
- 用户自带 MCP、个人 OAuth 和动态 SecretResolver；
- 多实例 lease/Redis；
- 全栈 async Agent；
- 修改 AgentLoop。

## 10. 预计 Agent 文件

新增：

- `src/assistant_agent/service/policy.py`
- `src/assistant_agent/service/capabilities.py`
- `tests/test_runtime_policy.py`
- `tests/test_mcp_startup_policy.py`
- `tests/test_runtime_capabilities.py`

修改：

- `src/assistant_agent/service/runtime.py`
- `src/assistant_agent/service/_runtime_builders.py`
- `src/assistant_agent/service/__init__.py`
- `src/assistant_agent/config/schema.py`
- `src/assistant_agent/mcp/manager.py`
- `src/assistant_agent/mcp/configure.py`（仅复用一次性探测时）
- `src/assistant_agent/skills/store.py` 或 discovery helper
- `src/assistant_agent/cli/setup.py`（只传兼容 policy 和展示能力，不复制装配）
- 配置示例、README、服务调用指南、ROADMAP 和技术债册。

明确不修改：

- `src/assistant_agent/agent/loop.py`
- RunState/checkpoint 状态机；
- LLM provider 抽象；
- API/Web 前端仓库。

`mcp/manager.py` 已超过 300 行软线。实现 M17b 前应先拆分连接/发现与调用策略，不能继续把健康、并行
和状态职责堆入同一文件；不放宽 500 行硬线。

## 11. 测试计划

### RuntimePolicy

- CLI 兼容 policy 下工具 Schema、Skill 来源和 sandbox 行为不回退；
- production policy 不注册两个扩展管理工具；
- personal Skill 不被发现，显式配置并信任的 Skill 可见；
- 禁止 stdio 时配置中的 stdio MCP 不启动并返回结构化状态；
- sandbox 低于 policy 下限时类型化失败；
- config 无法突破调用方 policy。

### MCP 启动

- 一个 optional 失败、一个成功：Runtime 成功且只注册成功工具；
- 全部 optional 失败：内置工具和普通对话仍可用；
- required 失败：当前 Runtime 类型化失败并清理所有已连接 server；
- 连接并行但最终工具顺序稳定；
- connect timeout 与 call timeout 独立；
- N 个慢 server 的总启动时间受并行上界约束；
- initialize、tools/list、schema 和名称冲突各自映射安全状态；
- close/init rollback 无线程、session 或子进程遗留。

### 能力快照

- 只包含脱敏 server/tool/skill 信息；
- 不含 headers、env、token、完整 command 或原始异常；
- connected/degraded/disabled/blocked/required_failed 映射稳定；
- Runtime notice 与快照一致；
- 独立导入不加载 CLI/UI。

### 集成回归

- Fake provider 下 CLI 和 service 分别完成 Session -> Run -> terminal sync；
- 双 Runtime policy/Workspace/MCP/权限状态不污染；
- paused Run 不因 MCP 后台恢复而改变 definition；
- 全量 pytest、覆盖率、Ruff、mypy 和架构适应度测试全绿。

## 12. 验收标准

1. CLI 与 Web 继续使用同一 Runtime Factory、SessionRuntime 和 AgentLoop；
2. Web 服务 policy 不能被 config、模型或 HTTP 请求放宽；
3. optional MCP 全部离线时，普通 Agent 仍能在有界时间内启动和对话；
4. required MCP 失败只阻断对应 Runtime/profile，不影响 API liveness；
5. 多 MCP 连接有界并行，工具 Schema 顺序稳定；
6. 前端可通过结构化快照判断可用、降级、禁用和策略阻断；
7. Web Runtime 不注册扩展管理工具，也不加载服务配置未允许的 personal Skill；
8. 不在 active/paused Run 热插拔工具；
9. 初始化失败与 close 后无 MCP 线程、子进程、HTTP client 或受管进程遗留；
10. 未修改 `agent/loop.py`；
11. 质量门和状态文档全部按实测更新；
12. API 仓库无需导入 Agent 的 cli/ui/private 模块。

## 13. 风险与决策

- **CLI 行为回退**：策略对象默认保持现状，CLI 显式使用兼容 policy，并用 Schema/Skill/MCP 回归固定。
- **并行导致工具顺序漂移**：连接结果按原配置顺序归并，不按完成顺序注册。
- **required 扩大故障域**：只在服务端明确 capability profile 中使用；默认 optional。
- **能力恢复但当前 Runtime 无工具**：明确使用 idle rebuild，不做隐式热插拔。
- **共享 stdio/MCP 资源压力**：首期通过 Runtime Pool 和服务级配额解决，不提前引入跨 Session 连接池。
- **服务器个人 Skill 意外注入**：service policy 从发现入口排除，而不是发现后只靠 UI 隐藏。
- **扩展配置竞态**：production policy 不注册修改工具；管理员配置走独立部署控制面。
- **未来账号扩展**：本期不写空壳用户字段；账号系统立项后在 API 层增加 ownership/workspace 映射，
  不改变 Agent Session/Run 核心契约。

## 14. 参考原则

OpenHands 可借鉴的原则：REST 作为控制面、WebSocket 作为事件面、每 Conversation 隔离运行服务、
同步实现进入受控 worker、事件可重放、断线不取消任务、终态晚于前序事件发布。当前项目不照搬其
远程 Agent Server、warm pool、Browser 直连 sandbox 或全量 async 复杂度。

- [OpenHands System Architecture](https://github.com/OpenHands/OpenHands/blob/1.6.0/openhands/architecture/system-architecture.md)
- [OpenHands Conversation Startup](https://github.com/OpenHands/OpenHands/blob/1.6.0/openhands/architecture/conversation-startup.md)
- [OpenHands Agent Server](https://docs.openhands.dev/sdk/arch/agent-server)

本方案已按 M17a -> M17b -> M17c 实施；`577 passed / 5 skipped`、覆盖率 84%，
Ruff/format/mypy 全绿，未修改 `agent/loop.py`。API 适配继续按双仓协同说明独立提交。
