# M21 受管命令与后台进程生命周期方案

> 状态：已完成（2026-07-19）
> 日期：2026-07-19
> 内核影响：**不修改 `agent/loop.py`**
> 公共契约：`ToolDisplay.timeout_seconds` 向后兼容扩展；`EVENT_CONTRACT_VERSION == 1`、
> Run checkpoint schema v3 均不变

## 1. 现场结论

本次不是根据终端动画猜测。现场 Run `run-20260719-112314-25c39468` 的 checkpoint 和
进程树共同证明：

- Run 保持 `running / tools_pending`，最后更新时间停在 11:25:23；
- 当前位于 iteration 14/15，工具调用 15/50，工具输出 11917/30000；
- 没有结构化 failure，也没有收到最后一次 `run_shell` 的 tool result；
- `server.py 8765` 已成为仍存活的后台进程，而启动它的外层 Shell 已退出；
- 本机 `shell_timeout` 是 60 秒，但 CLI 等待超过 4 分钟。

因此本次直接根因不是上下文超限，也不是 iteration/tool budget 耗尽，而是 Shell 进程收尾死锁。

`ProcessSupervisor.run()` 只在外层进程存活时检查 deadline。外层 `cmd.exe` 因 `start /b`
先退出后，代码立即离开 timeout 轮询，然后对 stdout/stderr 读取线程执行无超时 `join()`。后台
子进程继承了管道写句柄，读取线程收不到 EOF，Run 因而永久停在一次同步工具调用内部。

现有 M14 测试覆盖“父进程仍存活时 timeout 清理后代”，没有覆盖“父进程先退出、后代继承管道”
这一种 Windows/POSIX 都可能出现的形态。D18 因此属于不完整还债，需要重新打开。

## 2. 设计判断

### 2.1 已有机制有效但未命中本次问题

- Provider 请求已有 `request_timeout`，Provider timeout/context error 会映射成结构化 failure。
- iteration、tool call、tool output 已有预算和 continuation。
- checkpoint 会把未完成的 started tool call 在恢复时转成 `tool_uncertain`，不会自动重放。
- CLI 活动计时器没有卡死；它准确显示主线程仍阻塞在工具执行中。

### 2.2 不能只调配置

- 调低 `shell_timeout` 无效，因为当前死锁发生在 deadline 轮询退出之后。
- 给整个 Run 增加粗暴墙钟上限会误杀合法长任务，也无法安全抢占任意同步 Python Tool。
- 仅在 UI 增加“仍在运行”动画会掩盖故障，不会恢复 Run。
- 禁止模型写 `start /b` 只能减少触发概率，不能修复底层无限等待不变量。

### 2.3 前台命令与后台服务必须分开

`run_shell` 保持有界前台命令语义：命令结束、超时、暂停或取消后必须在有限时间内返回，并清理
它拥有的进程树。需要跨多个 Agent 步骤存活的开发服务器应使用显式受管后台进程能力，不再依赖
`start /b`、`nohup` 或 shell `&` 逃逸。

## 3. 范围

### 3.1 M21a：P0 前台执行终止保证

- deadline 覆盖外层进程等待、管道排空和进程树清理的完整生命周期。
- 删除所有无界 `process.wait()` / reader `thread.join()` 收尾路径。
- 外层进程退出后仅允许一个很短的管道排空窗口；仍未 EOF 时判定存在继承句柄的后代。
- Windows 通过 Job Object、POSIX 通过 process group 清理仍受管的后代，再关闭管道并有界等待。
- timeout/pause/cancel/正常退出均保证 `ProcessSupervisor.run()` 有限返回。
- 清理失败返回安全、结构化工具错误和诊断 metadata，不透传命令中的敏感内容或原始异常。
- `close()` 保持幂等，不遗留 reader 线程和受管子进程。

### 3.2 M21b：受管后台进程

- 增加显式后台进程工具能力：启动、查看状态/有界输出、停止、列出。
- 返回 Runtime 内部生成的 opaque process ID，不要求模型依赖 OS PID。
- 后台命令继续经过 Registry Schema 校验、权限策略、审计和 workspace/sandbox 边界。
- 每个 SessionRuntime 独立持有后台进程表；不同 Session 不共享进程授权或句柄。
- Runtime 关闭时清理全部后台进程；不得默认创建脱离 Agent 生命周期的 daemon。
- 输出使用现有有界捕获和 artifact 规则，避免内存、上下文和磁盘无限增长。
- 状态至少区分 `starting/running/exited/failed/stopped`，并提供安全退出码和截断日志摘要。
- 恢复旧 checkpoint 时不自动重启后台进程；不可恢复的旧 process ID 明确返回 unavailable。
- 后台启动在 checkpoint 中仍按有副作用工具处理；崩溃在 started/completed 边界时沿用
  `tool_uncertain`，不得自动重放。

工具命名在实施时优先保持少而清晰：前台仍为 `run_shell`，后台生命周期使用一个聚合的
`manage_process` 工具和受约束 action，避免为每个动作增加一份大 Schema。

### 3.3 M21c：安全运行反馈

- CLI 在执行 Shell/后台进程动作时显示动作类型、已等待时间和可用的暂停提示。
- 前台 Shell 的安全展示包含配置的最大执行时间，但不展示完整敏感命令。
- 日志记录 execution、drain、cleanup 三段安全耗时和 termination reason，便于区分模型慢、工具慢、
  清理慢。
- API 仍负责 heartbeat；Agent 不复制网络心跳或 WebSocket 状态机。

### 3.4 不做

- 不修改 `agent/loop.py`。
- 不做全栈 async，不用线程强杀任意 Python Tool。
- 不增加粗暴的 Run 总墙钟超时。
- 不改变 context/iteration/tool budget 的既有语义。
- 不允许后台进程绕过权限、sandbox 或 Runtime close。
- 不修改 assistant_agent_api / assistant_agent_web。

## 4. 预计代码结构

- `execution/process.py`：前台进程完整 deadline、排空与清理不变量。
- `execution/process_windows.py`：Job 状态/终止的窄接口，避免调用方直接碰 ctypes。
- `execution/jobs.py`：SessionRuntime 级后台进程所有权和状态机。
- `execution/workspace.py`：前台/后台执行统一经过 Workspace 边界。
- `tools/shell.py`：前台命令错误与展示语义。
- `tools/processes.py`：`manage_process` Tool adapter。
- `bootstrap/tools.py`、`bootstrap/runtime.py`：装配同一个进程管理器，不复制 CLI/API 路径。
- `contracts/capabilities.py`：如需公开能力快照，只做向后兼容字段扩展。
- `ui/`：安全等待反馈，不承载执行状态机。

最终文件以现有所有权为准；不为追求文件数量预先拆空模块。

## 5. 测试计划

### 5.1 确定性回归

- 父进程先退出、后代继承 stdout/stderr：必须在有界时间返回，Windows 实机覆盖 `start /b`。
- POSIX `sh -c '... &'` 等价场景进入 CI。
- 正常短命令不丢 stdout/stderr，双流大输出仍不死锁。
- timeout、pause、cancel 均清理后代和 reader 线程。
- 清理异常不覆盖原始 termination reason，且不泄漏命令参数。
- `ProcessSupervisor.close()` 重复调用安全。

### 5.2 后台进程

- start -> running -> bounded logs -> stop -> stopped。
- 进程自然退出、启动失败、重复 stop、错误 process ID。
- 不同 Runtime 的 process ID、进程表和权限互不污染。
- Runtime close 清理全部后台进程，无线程和进程遗留。
- workspace/container 边界与网络默认值不回退。
- checkpoint 恢复不自动重启，started 调用仍进入 uncertain 决策。

### 5.3 全量门禁

- pytest、coverage、Ruff format/check、mypy、import-linter 全绿。
- scripted/recovery eval 全绿，事件终态顺序不回退。
- Windows 真实进程树测试必须通过，Linux CI 路径保留。

## 6. 公共契约影响

- StepEvent v1、Run checkpoint v3 和 failure code 默认保持不变。
- 新工具 Schema 属 Runtime capability 的向后兼容增加；API 不得写死工具清单。
- 若增加 capability/status 字段，同步正式服务契约、契约测试和 API AI 交接文档。
- 若实现中发现必须改变 checkpoint，停止实施并单独给出迁移方案，不静默提升版本。

## 7. 验收标准

1. 截图中的 `start /b` 继承管道场景不再无限等待。
2. `shell_timeout=60` 时，前台工具在“60 秒 + 有界清理宽限”内返回。
3. Agent 能用显式后台工具启动测试服务、查询、读取日志并停止。
4. Ctrl+C 在前台命令和后台管理操作中均保持现有 pause/cancel 语义。
5. 任何 Runtime 关闭后不遗留其拥有的进程或 reader 线程。
6. CLI 与 Service 入口复用同一执行实现。
7. 不修改 `agent/loop.py`，全量质量门和 eval 不回退。

## 8. 临时处置

修复发布前，遇到当前卡住的 Run 应先用 Ctrl+C 请求暂停；若无法退出，再第二次 Ctrl+C 强制取消。
当前 `server.py 8765` 是已启动的独立进程，结束 Agent 不等于它已停止，需要用户明确决定是否终止。
不要继续用 `start /b`、`nohup` 或 `&` 让服务跨工具调用存活。

## 9. 实施与验收结果

- 前台命令 deadline 已覆盖 execution、drain 和 cleanup；Windows `start /b`、POSIX shell 后台及
  通用父进程退出后代继承 PIPE 均有确定性回归。
- 新增 Runtime 隔离的 `manage_process`，支持 `start/status/logs/stop/list`，opaque ID、输出、
  进程数量和完成历史均有界；Runtime close 和初始化失败回滚会清理后台进程。
- container Workspace 明确返回 `managed_process_container_unsupported`，未退回宿主执行；二次逃逸
  后代会被清理并返回 `managed_process_detached_child`。
- CLI 展示安全的最长等待时间，不展示完整敏感命令；后台进程状态机没有进入 UI 或 API 层。
- `637 passed / 6 skipped`，覆盖率 84%；Ruff format/check、mypy、12/12 import-linter、
  scripted eval 18/18、recovery eval 4/4 全绿。
- 生产 Python 为 14,897 行/125 文件，eval 基础设施 1,404 行；`agent/loop.py` 无改动。
- 正式公共契约已同步到 `docs/agent-service-integration-guide.md`，API 交接见
  `m21-agent-api-handoff.md`。

## 10. 契约结论

本里程碑存在公共向后兼容扩展：`ToolDisplay.timeout_seconds` 为可选字段，Runtime 工具清单可能包含
`manage_process`。没有删除字段、改变事件顺序或修改 checkpoint；因此事件契约保持 v1，checkpoint
保持 v3。进程 ID 仅在当前 Runtime 生命周期内有效，调用方不得复制后台进程状态机或持久化后跨
Runtime 恢复。
