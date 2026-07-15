# M10c 决策：异步与可取消运行时

> 日期：2026-07-15
> 结论：**NO-GO 全栈 async 重构；GO 独立评估进程树监管，其他异步迁移等待触发信号。**

## 1. 决策问题

当前 `Tool.run()`、`AgentLoop.run()` 和 CLI 都是同步协议；MCP 通过守护线程中的 asyncio loop
桥接，Shell/Git 通过阻塞 `Popen.wait()` 执行。M10c 判断是否应立即把循环和全部工具改为 async，
以及“可取消”是否必须依赖这次重构。

## 2. 现状证据

- `tools/process.py` 的 timeout 只 `kill()` 直接子进程。使用 `shell=True` 时，后代进程可能继续
  存活，D18 属实，但这是进程所有权问题，不是函数是否写成 `async def` 的问题。
- `mcp/manager.py` 的 `run_coroutine_threadsafe()` 桥已有超时取消、连接回滚和关闭测试；当前没有
  高频故障记录，也没有把本项目嵌入既有 asyncio 服务的需求。
- `AgentLoop` 已能在流式响应和工具批次边界响应 Ctrl+C，但同步 Tool 运行期间只能等待工具返回或
  timeout。现有记录没有证明该问题已经高频影响使用。
- 当前不存在并行执行只读工具的行为基线，不能证明并行收益足以覆盖权限、预算、日志顺序和
  checkpoint 确定性带来的复杂度。

## 3. 外部事实

1. [Python subprocess 文档](https://docs.python.org/3/library/subprocess.html) 提供 POSIX
   `start_new_session` / `process_group` 和 Windows creation flags，但 `Popen.kill()` 本身只处理
   被持有的进程，不等于跨平台进程树监管。
2. [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
   可把关联进程作为一个单元管理；`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 可在关闭最后一个 Job
   handle 时终止关联进程。嵌套 Job、breakaway 和 handle 生命周期必须专门测试。
3. [Python asyncio 多线程指南](https://docs.python.org/3/library/asyncio-dev.html) 建议阻塞工作走
   executor，并用 `run_coroutine_threadsafe()` 跨线程提交协程；这与当前 MCP 桥方向一致。
4. [`asyncio.to_thread()` 实现](https://github.com/python/cpython/blob/main/Lib/asyncio/threads.py)
   只是 `run_in_executor()` 包装。取消等待它的 Task 不会强制停止已经运行的同步函数，因此
   `Tool.run_async()` 默认包线程池只能提供兼容迁移，不能单独兑现强取消。

## 4. 方案比较

| 方案 | 收益 | 主要代价/风险 | 决策 |
|------|------|---------------|------|
| 一次性 async 重写 Loop、Registry、全部 Tool、CLI | API 形式统一 | 回归面最大；同步工具仍不可强停；破坏 M10b 顺序/checkpoint 语义 | 拒绝 |
| async 核心 + 同步兼容入口 + `run_async()` 默认线程池 | 可渐进迁移、便于未来嵌入 | 线程内旧工具仍不可取消；双入口测试成本上升 | 等触发 |
| 先做跨平台 ProcessSupervisor | 直接解决 D18；不动 Loop 协议 | Windows Job Object 与 POSIX process group 需平台专测 | 推荐独立立项 |
| 保持现状 | 零回归 | D18 和工具执行中 Ctrl+C 延迟继续存在 | 当前默认，明确披露边界 |

## 5. 正式决策

1. 第三阶段不实施全栈 async，不修改 `agent/loop.py`、Tool 基类或 MCP 桥。
2. D18 保持未还清；建议下一窄里程碑暂命名“跨平台进程监管”，需用户另行确认后实施：
   POSIX 使用新 session/process group 并分级 TERM/KILL；Windows 使用 Job Object 管理后代进程。
3. “进程取消”与“协程取消”分开建模。未来 `CancellationToken` 可进入 `ToolContext` 供协作式工具
   轮询；外部进程由 ProcessSupervisor 持有和终止；不能把取消 asyncio Task 当作副作用已经停止。
4. 若进入异步迁移，采用兼容顺序：先加 async 核心和同步 facade，再原生迁移 MCP/HTTP，最后才
   评估只读工具并行。旧同步 Tool 通过线程池兼容，但文档明确其取消上限。
5. 并行只允许无副作用且权限已通过的调用；预算预留、结果顺序、日志 call ID 和 M10b checkpoint
   必须先定义，禁止直接 `gather()` 任意工具调用。

## 6. 重新立项触发条件

满足任一项才细化 async 实施方案：

- 30 天内出现至少 2 次长工具无法及时停止的可复现问题，且 ProcessSupervisor/协作式取消不足以解决。
- 确定性基准显示一轮 3 个以上独立只读 I/O 工具的串行执行占任务 p95 延迟 30% 以上，并行可稳定
  降低至少 20%，且不破坏权限、预算和恢复语义。
- MCP 桥出现重复的 event-loop 生命周期/清理故障，无法在桥内部修复。
- 出现明确的库/服务嵌入需求，调用方必须在既有 asyncio loop 中并发运行多个 Agent。

## 7. 后续实现验收边界

跨平台进程监管立项时至少覆盖：Windows 与 Linux、父进程派生孙进程、超时、Ctrl+C、正常退出、
双流持续输出、进程自行脱离/失败降级、无残留 PID；不得只用 mock 声称 D18 已还清。async 迁移
立项时必须额外证明旧同步 API 兼容、M10b checkpoint 顺序不变、取消后副作用状态可判定或明确标为
不确定。

## 8. 结果

M10c 决策门完成。它没有新增运行时能力，也没有还清 D18；其价值是把“强取消”“进程树回收”与
“异步 API/并行吞吐”拆成可验证的不同问题，避免以重构规模代替需求证据。
