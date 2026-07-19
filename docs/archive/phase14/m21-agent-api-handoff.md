# M21 Agent -> API 交接

> 日期：2026-07-19
> Agent 基线 commit：`c07fa32a3c3ae3a2e915298a7fd2f50a0a26caf3`
> M21 implementation：本提交（提交后由接入方固定实际 commit/wheel）
> 公共事件契约：`EVENT_CONTRACT_VERSION == 1`（不提升）
> Run checkpoint：schema v3（不变）

## 1. 给 assistant_agent_api AI 的执行说明

先完整阅读 Agent 安装版本中的 `docs/agent-service-integration-guide.md`，再按本文件调整 API。
不要修改 Agent 仓库，不要复制受管进程、Session/Run、恢复或 checkpoint 状态机。

M21 修复前台 Shell 父进程先退出、后台后代继承 PIPE 导致 Run 永久停在 `tools_pending` 的问题，
并提供 Runtime 隔离的 `manage_process`。StepEvent 终态、Interaction、恢复和 checkpoint 语义不变。

## 2. API 必改项

1. 更新并固定包含 M21 的 Agent wheel/commit，继续断言 `EVENT_CONTRACT_VERSION == 1`。
2. 事件 DTO 接受 `ToolDisplay.timeout_seconds: float | None`。字段缺失保持兼容；字段存在时仅用于
   展示安全最长等待时间，不向客户端发送完整命令来推断 timeout。
3. 工具清单必须来自当前 Runtime capability，不得写死 `manage_process`。上下文 Schema 预算不足、
   policy 限制或未来配置变化时，该工具可以不注册，API readiness 不应因此失败。
4. Session 淘汰、Runtime 初始化失败回滚和服务 shutdown 必须调用公共 `SessionRuntime.close()` /
   `AgentRuntime.close()`，并等待既有关闭流程完成。不能只停止 Run worker。
5. 不在 API 建立第二套后台进程 registry，不保存 OS PID，也不从工具结果恢复完整命令或环境变量。
6. `proc-<12 hex>` 只在创建它的 Runtime 生命周期内有效。不要跨 Runtime、进程重启或 SessionRuntime
   淘汰持久化；旧 ID 应按 unavailable 处理，不自动重启。
7. 以下值按结构化工具结果处理，不解析 `message`：
   `background_process_detected`、`managed_process_container_unsupported`、
   `managed_process_detached_child`。
8. 后台 `start` 仍是有副作用工具。若 checkpoint 位于 started/completed 边界，继续使用 Agent 既有
   `tool_uncertain` Interaction，API 不得自动 retry。

## 3. 公共字段与安全边界

`ToolDisplay` 新增字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `timeout_seconds` | `float | None` | 可展示的工具最长等待时间；向后兼容可选字段 |

`manage_process` 支持 `start/status/logs/stop/list`。工具结果 metadata 只允许消费安全字段：

- `process_id`
- `status`
- `returncode`
- `elapsed_seconds`
- `stdout_bytes`
- `stderr_bytes`

API/Web 不应展示或持久化 OS PID、完整命令、环境变量、原始异常或未脱敏工具参数。日志内容仍按
普通工具输出执行现有长度限制、脱敏和敏感事件过滤。

## 4. 兼容性与迁移

- **破坏性变化：无。**
- **公共 additive 变化：有。** `ToolDisplay.timeout_seconds` 为可选字段，工具 capability 可新增
  `manage_process`。
- **事件契约：** v1 不变；没有删除字段，也没有改变 `final -> run_terminal` 顺序。
- **checkpoint：** v3 不变；后台进程句柄不进入 checkpoint，不迁移旧 ID，不自动重启。
- **API 数据迁移：** 不需要数据库迁移。若 API 使用严格 DTO，必须先增加可选字段再升级 Agent 包。

## 5. 联调事件与工具场景

### 5.1 前台命令正常完成

```text
tool_call(run_shell, display.timeout_seconds=60)
tool_result(call_id 相同, success=true)
...正常 StepEvent 流...
final
run_terminal(completed)
```

### 5.2 Shell 误启后台后代

```text
tool_call(run_shell, display.timeout_seconds=60)
tool_result(
  call_id 相同,
  success=false,
  result_code=background_process_detected
)
模型可选择调用 manage_process(start)，Run 本身不因此自动 failed
```

### 5.3 显式后台进程

```text
manage_process(start) -> process_id=proc-xxxxxxxxxxxx, status=running
manage_process(logs)  -> 同一 Runtime 内返回有界日志
manage_process(status)-> running/exited/failed/stopped
manage_process(stop)  -> stopped
SessionRuntime.close  -> 清理仍存活的所有受管进程
```

### 5.4 容器 Workspace

```text
manage_process(start)
  -> success=false
  -> result_code=managed_process_container_unsupported
  -> 不退回宿主执行，不把错误文本解析为 retry
```

### 5.5 不确定副作用恢复

```text
manage_process(start) 已进入 started checkpoint 后进程崩溃
  -> 恢复原 run_id
  -> tool_uncertain Interaction
  -> 用户明确 retry/skip/abort
  -> timeout/断线默认不 retry
```

## 6. API 联调验收

1. 旧事件没有 `timeout_seconds` 时 DTO 仍能反序列化；新字段存在时能安全展示。
2. Runtime 有/无 `manage_process` 两种 capability 均可创建 Session 和运行普通任务。
3. 前台误启后台后代在 deadline 加有界清理时间内返回，不让 API worker 永久占用。
4. 两个 SessionRuntime 的 process ID 与状态互不可见。
5. Runtime close 后旧 process ID 不可用，后台进程和 reader 线程无遗留。
6. API shutdown、Session 淘汰和初始化失败路径均覆盖 close 测试。
7. 三个结构化 result code 不依赖中文文本分类，也不升级为错误的 Run terminal。
8. `tool_uncertain` 继续保持精确 request ID、超时拒绝和不自动重放。
9. `final -> run_terminal(completed)`、failed/paused/cancelled 终态顺序测试继续全绿。
10. 网络 DTO 不包含 OS PID、完整命令、环境变量、原始异常或敏感工具参数。

## 7. Web 影响

Web 可选展示工具最长等待时间和后台进程安全状态，但不应提供“按 PID 管理”或跨会话恢复按钮。
本次不要求 Web 新增页面；API 若转发工具事件，继续使用现有脱敏 `ToolDisplay` 和 result metadata。
