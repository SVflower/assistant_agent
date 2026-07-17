# M14 受控执行运行时方案

> 状态：实施中（2026-07-17）。用户已授权修改 `agent/loop.py`；M14a 已完成，M14b/M14c
> 待实施。

## 1. 背景与目标

当前项目已经具备统一权限门、预算、审计、步骤级 checkpoint、Run 恢复和工具超时，但这些能力
仍属于应用层控制：

- 权限确认决定“是否允许执行”，不能限制获准进程拥有的 OS 权限；
- Ctrl+C 只在模型流片段之间和工具批次开始前被检查，工具运行期间不能可靠响应；
- Shell 超时只终止直接 `Popen` 进程，派生进程可能继续运行（D18）；
- Run 恢复描述持久化状态，不提供进程内的暂停、取消和资源清理控制面；
- 本地文件、Shell、Git、Web 和外置 MCP 运行在不同边界，当前不能笼统宣称“已沙盒化”。

M14 的目标是让通用 Agent 执行任意任务时具备可解释、可测试的停止与隔离边界：

1. 用户可以暂停或强制取消正在执行的任务；
2. Agent 能终止自己启动并受管的完整进程树；
3. 内置工具通过统一 Workspace 接口访问执行环境；
4. 在宿主模式之外提供可选容器沙盒，并明确无法覆盖的外部边界。

本阶段不把项目改造成专业代码 Agent，也不增加代码索引、子 Agent 或复杂任务编排。

## 2. 调研结论

### 2.1 OpenHands Software Agent SDK

主要参考 MIT 许可的
[OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk)，而不是旧主仓库的
前端和云服务：

- `conversation/cancellation.py` 使用 `threading.Event` 实现同步/异步均可检查的协作式取消；
- `conversation/state.py` 把 Conversation 生命周期与无状态 Agent 推理分开；
- `workspace/base.py` 统一命令、文件、暂停/恢复和资源清理；
- `openhands-workspace` 提供 Local、Docker 和 Remote 实现；
- pause 示例证明不进行全栈 async 重构也可以先建立运行控制面。

不直接照搬的部分：OpenHands 的本地 PTY Terminal 明确不支持 Windows；取消令牌仍依赖工具主动
检查；其 `interrupt()` 更接近暂停，没有充分区分“可恢复暂停”和“终止任务”。本项目必须保留
Windows 一等支持，并与已有 RunState/RunStore 合并，而不是复制第二套 Conversation。

### 2.2 平台进程能力

- POSIX 使用独立 session/process group，并对进程组发送 TERM/KILL；
- Windows 使用 Job Object 的 `KILL_ON_JOB_CLOSE`/`TerminateJobObject` 约束继承子进程；
- Python `Popen.kill()` 只针对直接进程，不能作为进程树清理证明；
- 容器沙盒必须同时约束挂载、环境变量、网络、资源和销毁流程，单纯限制 cwd 不是 OS 沙盒。

## 3. 现状评估

### 已有可复用基础

- `main._interruptible()` 已把 SIGINT 转成线程安全标志；
- `AgentLoop` 已在模型流和工具计划边界检查中断；
- `RunCoordinator` 已能在模型、审批和工具边界持久化 checkpoint；
- `ToolContext` 是向所有工具注入运行时能力的稳定入口；
- `tools/process.py` 已实现 stdout/stderr 并发 drain、有界捕获和超时；
- `Runtime.close()` 已统一关闭 MCP、Web 与日志；
- 权限策略已经区分文件、进程、网络与 MCP capability。

### 关键缺口

1. `execute_tool_batch()` 不在每个工具前后检查中断；一次批量调用会继续执行后续工具。
2. `run_bounded_process()` 在 `wait(timeout)` 中阻塞，无法响应中断，并且只 kill 直接进程。
3. MCP 同步桥等待 `Future.result(timeout)`；Web/LLM 同步请求最多只能等自身超时。
4. 当前全局 `_interrupt` 只表达布尔值，不能区分第一次暂停请求与第二次强制取消。
5. `RunStatus` 没有 `cancelled`，历史方案将所有人工中断都保存为 `paused`。
6. 文件工具直接使用 `Path`，Shell/Git 直接使用宿主进程，尚无可替换 Workspace 边界。
7. 外置 MCP server 可能运行在宿主机；即使内置工具进入容器，也不能宣称 MCP 被同一沙盒约束。

架构适合增量演进，不需要替换 LiteLLM、Registry、RunStore 或进行全栈 async 重构。

## 4. 总体设计

新增低层 `assistant_agent.runtime` 包，架构 rank 设为 1，只依赖 config/obs/标准库；tools、mcp、
agent 和 cli 可以向下依赖它：

```text
CLI signal/input
      |
      v
RunControl -----> AgentLoop safe points
      |                    |
      v                    v
CancellationToken --> ToolContext --> Registry/Tools/MCP/Web
      |
      v
ProcessSupervisor --> HostWorkspace / ContainerWorkspace
```

`RunState` 继续负责可序列化恢复状态；`RunControl` 只负责当前进程内控制。两者不能合并：线程事件
不可序列化，checkpoint 也不能终止活进程。

## 5. M14a：可靠中断与跨平台进程监管（已完成）

### 5.1 RunControl

新增线程安全的 `RunControl`：

- 每次 `run()`/`resume()` 创建或 reset 一次；
- 第一次 SIGINT：`request_pause()`，要求在最近安全边界停止并保存可恢复 checkpoint；
- 第二次 SIGINT：`request_cancel(force=True)`，强制终止受管进程并把 Run 标记为 `cancelled`；
- 请求幂等，状态只能单向升级：running -> pause_requested -> cancel_requested；
- signal handler 只置位，不执行日志、I/O、锁等待或进程清理。

`CancellationToken` 作为只读视图注入 `ToolContext`。声明式工具可通过 `ctx` 主动检查；不接收
`ToolContext` 的第三方同步函数只能在调用边界停止，不能宣称支持运行中取消。

### 5.2 中断边界

中断检查覆盖：

1. 模型调用前和每个流式片段后；
2. 工具批次开始前、每个工具开始前和结束后；
3. 权限等待前后；
4. Shell/Git 进程轮询期间；
5. MCP Future 等待期间；
6. Web 流式读取块之间。

同步 LLM/Web 调用在底层阻塞且尚未返回数据时，协作令牌不能抢占第三方库。为保证有界等待，
Provider 增加显式 `request_timeout` 并传给 LiteLLM；Web/MCP 继续保留现有超时。文档必须区分
“立即置中断请求”和“最迟在第三方请求超时/下一安全边界生效”。不以后台线程泄漏换取伪即时取消。

### 5.3 ProcessSupervisor

把 `tools/process.py` 的进程生命周期抽到 `runtime/process.py`：

- 保留现有双流 drain、head/tail 有界捕获和 artifact 契约；
- 使用短周期 wait 轮询 timeout 与 CancellationToken；
- POSIX：`start_new_session=True`，先 TERM 进程组，宽限期后 KILL；
- Windows：创建并绑定 Job Object，任务结束/超时/取消时终止整个 Job；
- 正常、超时、取消、启动失败路径都关闭句柄、wait 直接进程并 join drain 线程；
- `BoundedProcessResult` 增加稳定 `termination_reason`：completed/timeout/cancelled/failed；
- Supervisor 跟踪活跃进程，`Runtime.close()` 做最终兜底清理。

不引入 pywin32；Windows Job Object 使用窄范围 `ctypes` backend，并由平台测试验证。平台不支持时
必须 fail closed 或明确报告降级，不能静默宣称已终止进程树。

### 5.4 RunState 与恢复语义

RunState schema 升级到 v2：

- `RunStatus` 增加 terminal 状态 `cancelled`；
- v1 文档确定性迁移到 v2，原 `paused` 语义保持不变；
- pause：保存当前位置，可通过 `resume` 继续；
- cancel：终止本次 Run，不允许 resume；已经发生的外部副作用不回滚；
- 运行中工具被强制停止时记录 `executed=true`、`code=cancelled`，禁止自动重放；
- 模型已经规划但尚未执行的工具保持 planned，pause 可恢复，cancel 则统一终止且不执行。

Chat 中存在 paused Run 时，不允许静默开始下一任务造成 Session/Run 分叉；必须先 resume、cancel
或显式开启新会话。

### 5.5 M14a 文件边界

预计新增/修改：

- 新增 `runtime/control.py`、`runtime/process.py`；
- 修改 `tools/base.py`、`tools/process.py`、`tools/shell.py`、`tools/git.py`；
- 修改 `agent/execution.py`、`agent/run_state.py`、`agent/recovery.py`；
- **修改 `agent/loop.py`**：只增加安全边界检查和状态收尾，不重写 ReAct；
- 修改 `llm/client.py`、`mcp/manager.py`、`mcp/tool.py`、`web/client.py`；
- 修改 `cli/setup.py`、`main.py` 和必要的 UI 提示；
- 更新 config schema/example、日志事件、恢复命令和测试。

### 5.6 M14a 实施结果

- 新增线程安全 RunControl；第一次 SIGINT 暂停，第二次升级为强制取消；
- RunState v2 增加 terminal `cancelled` 并支持 v1 确定性迁移；
- 工具批次在每个调用边界停止，强制取消会为未执行调用补齐稳定结果；
- ProcessSupervisor 使用 Windows Job Object / POSIX process group 管理进程树；
- Shell/Git/MCP/Web 接入控制信号，模型 provider 增加显式请求 timeout；
- Windows 真实父子进程存活探针通过；全量 509 passed、3 skipped，Ruff/mypy 全绿。

## 6. M14b：Workspace 执行抽象

M14a 稳定后再引入 `BaseWorkspace`，避免进程控制和文件抽象同时落地：

```python
class BaseWorkspace(Protocol):
    root: Path
    def read_text(...): ...
    def write_text_atomic(...): ...
    def list_dir(...): ...
    def execute(...): ...
    def close(...): ...
```

第一批实现：

- `HostWorkspace`：兼容当前行为；权限策略仍可询问 workspace 外路径；
- `ConfinedWorkspace`：应用层强制限制在 root 内，越界直接拒绝，不提供“确认后越界”。

内置 Read/Write/Edit/List/Search/Shell/Git 逐步改为依赖 Workspace；Registry 和 Tool schema 不变。
原子写、有界读取、artifact 与权限目标继续复用现有实现。

边界声明：`ConfinedWorkspace` 是应用层路径约束，不是 OS 沙盒。自定义 Python Tool、外置 MCP、
Skill 脚本和获准宿主进程仍可能绕过；UI 和文档不得显示“完全隔离”。

M14b 原则上不修改 `agent/loop.py`。

## 7. M14c：可选容器沙盒

在 Workspace 契约稳定后增加 `ContainerWorkspace`：

- Docker/Podman 能力探测，缺失时明确失败或按用户配置回退；
- 默认只挂载当前 workspace，读写模式显式配置；
- 不挂载宿主 HOME、SSH、云凭据和 Docker socket；
- 默认最小环境变量、非 root 用户、`--network none`；
- CPU、内存、PID、磁盘/产物上限和任务超时可配置；
- 启动健康检查，结束后无论成功、异常或 Ctrl+C 都销毁容器；
- 产物只通过受控 workspace 路径导出。

外置 MCP 仍默认运行在宿主边界。容器模式下必须逐 server 显示其运行位置；宿主 MCP 不继承容器
隔离，不能因为 Agent 的内置工具在容器中就自动信任。将 MCP server 移入容器属于后续独立能力。

容器实现不修改 Agent Loop；真实 Docker 测试标记为平台能力测试，没有 Docker 时 skip，确定性
单测仍必须覆盖命令构造、挂载、环境和清理策略。

## 8. 明确不做

- 不进行全栈 async 重构，不推翻 M10c 决策；
- 不复制 OpenHands Conversation、远程 Agent Server 或 WebSocket 服务；
- 不实现多 Agent、后台任务队列、定时任务或 Prompt Queue；
- 不把权限策略宣传为沙盒，也不把 cwd/path 校验宣传为 OS 隔离；
- 不承诺强制停止可以回滚已发生的文件、网络或业务副作用；
- 不把业务 MCP 搬进 Agent 仓库；
- 不为所有任意第三方 Tool 提供不可证明的抢占式取消。

## 9. 测试计划

### M14a

- RunControl 状态升级、幂等、并发读取和 reset；
- 模型前/流中/工具批次前/批次内中断，不执行中断后的工具；
- pause checkpoint 可恢复，cancel terminal 不可恢复，v1 -> v2 迁移；
- Shell 正常、timeout、pause、force cancel 的结果与日志；
- 父进程派生子进程后，Windows Job Object/POSIX process group 均不留存活子进程；
- drain 线程、句柄、MCP Future 和 Runtime close 无泄漏；
- Ctrl+C 第一次暂停、第二次强制取消的 CLI 行为；
- Provider timeout 透传且不写死 provider。

### M14b

- HostWorkspace 行为与当前逐字节兼容；
- ConfinedWorkspace 拒绝 `..`、绝对路径、symlink/junction 逃逸和 TOCTOU 明显路径；
- 文件原子写、搜索、Shell cwd、Git 和 artifact 均使用同一 root；
- 自定义 Tool/MCP 不受 Workspace 保护时有明确能力标识和警告。

### M14c

- Docker/Podman 命令不经 shell 拼接；
- 默认无网络、无 HOME/凭据/Docker socket 挂载、非 root、资源限制存在；
- 正常/异常/中断均清理容器；
- workspace 外文件不可见；
- 有 Docker 的本地真实安装、执行、取消、销毁闭环。

每个子里程碑均执行 `pytest -q --cov`、`ruff format --check .`、`ruff check .`、`mypy src`，并
增加 Windows/Linux CI 覆盖。进程树测试必须使用最终存活探针，不只断言 API 被调用。

## 10. 验收标准

### M14a 完成

1. 长 Shell 运行中 Ctrl+C 能停止完整受管进程树并在有界时间返回；
2. 第一次中断产生可恢复 paused Run，第二次强制取消产生 terminal cancelled Run；
3. 工具批次中断后不再执行后续工具；
4. MCP/LLM/Web 的不可抢占边界和最大等待时间可见；
5. D18 在 Windows/POSIX 真实测试通过后还清；D20 未覆盖部分继续保留；
6. 现有恢复、不重放、权限、预算和双后端行为不回退。

### M14b 完成

1. 所有内置文件/进程工具通过 Workspace；
2. 默认 HostWorkspace 不破坏现有用户行为；
3. ConfinedWorkspace 不能通过路径或链接逃逸；
4. UI 明确区分应用层约束与 OS 沙盒。

### M14c 完成

1. 用户可仅改配置选择容器执行环境；
2. 容器缺失、启动失败和清理失败都有稳定错误，不静默降级；
3. 默认容器看不到宿主 workspace 外文件和敏感凭据；
4. Agent 退出后不遗留本阶段创建的容器或受管进程。

## 11. 实施顺序与审阅门

1. **先确认 M14 总体边界，并单独授权修改 `agent/loop.py`。**
2. 实施 M14a：RunControl -> ProcessSupervisor -> Loop/Tool/MCP 接线 -> RunState v2 -> CLI/eval。
3. M14a 全绿并单独提交、归档后，再开始 M14b。
4. M14b 全绿并单独提交后，进行 Docker/Podman 可用性探测和 M14c 真实环境验收。
5. 每个子里程碑独立更新 ROADMAP、TECH_DEBT、状态数字和方案实施结果；完成后归档到对应阶段。

### 风险边界

- **Windows Job Object 兼容性**：使用窄接口 ctypes，覆盖句柄关闭和已有 Job 限制；无法证明时不还 D18。
- **强制取消的副作用**：只能停止后续执行，不能回滚已发生动作；结果必须标记 executed。
- **LLM 阻塞**：同步第三方调用只能由 provider timeout 保证上界，不承诺毫秒级抢占。
- **沙盒错觉**：ContainerWorkspace 只覆盖经它执行的内置工具；宿主 MCP/自定义 Tool 单独展示边界。
- **改动面过大**：M14a/M14b/M14c 分提交、分验收，任一阶段不以“顺手重构”为由扩大范围。
