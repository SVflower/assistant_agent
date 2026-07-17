# M16 给 assistant_agent_api 的正式接入交付

> 交付日期：2026-07-17
>
> Agent 侧状态：公共服务边界已实现；未修改 `agent/loop.py`；API 可以开始正式开发。

## 1. 安装与允许导入的模块

开发期建议由 API 项目的虚拟环境安装本地 Agent 包：

```powershell
pip install -e D:\Dev\AI\assistant_agent
```

API 只应依赖以下公共出口：

```python
from assistant_agent.interaction import (
    ApprovalDecision,
    BlockingInteractionPort,
    ContinueDecision,
    DefinitionChangeDecision,
    QuestionAnswer,
    RecoveryDecision,
)
from assistant_agent.service import (
    AgentService,
    EVENT_CONTRACT_VERSION,
    SessionBusyError,
    SessionRunConflictError,
    StepEvent,
)
```

不要导入 `assistant_agent.cli`、`assistant_agent.ui`，也不要直接复制 Session 同步、恢复兼容检查或
checkpoint 状态机。

## 2. 最小运行流程

```python
from pathlib import Path

from assistant_agent.interaction import BlockingInteractionPort
from assistant_agent.service import AgentService

service = AgentService(
    config_path=Path(r"D:\server-config\assistant-agent.yaml"),
    workspace_root=Path(r"D:\server-workspaces\session-owner-root"),
)

interaction = BlockingInteractionPort(timeout=60.0)
session_runtime = service.create_session(
    interaction=interaction,
    interactive=True,
)

execution = session_runtime.start_run("读取项目状态并给出摘要")
run_id = execution.run_id

try:
    for event in execution.events:
        # API 在这里增加 seq/timestamp/session_id/run_id，并写入自己的重连缓存。
        publish_agent_event(event)
finally:
    session_runtime.close()
```

`config_path` 和 `workspace_root` 必须由服务端决定，不能来自单次聊天消息。Runtime 不修改全局
`os.chdir()`；相对日志、Run、Skill 和 MCP cwd 均相对固定 workspace 解析。

## 3. Session 与 Run API

```python
# Session CRUD
runtime = service.create_session(interaction=port)
runtime = service.load_session(session_id, interaction=port)
sessions = service.list_sessions()
deleted = service.delete_session(session_id)

# Run
execution = runtime.start_run(task)
runtime.pause()                 # 线程安全请求暂停
runtime.cancel()                # 线程安全请求取消
execution = runtime.resume_run(run_id)  # 沿用原 run_id

# 查询
unfinished = runtime.unfinished_runs()
runs = service.list_runs(session_id=session_id)
service.prune_completed_runs()
```

约束：

- 一个 `SessionRuntime` 对应一个隔离的 Runtime、Conversation、RunControl、权限记忆和 MCPManager；
- 同一个 Session 同时只能有一个 Run，冲突抛 `SessionBusyError`；
- Session 有 paused/running Run 时不能创建新 Run，抛 `SessionRunConflictError`；
- 不同 SessionRuntime 可放到不同受控工作线程执行；
- API 应缓存活跃 SessionRuntime，不要每条流式事件重建 Runtime；
- Run terminal 后自动原子同步 Session，成功后才设置 `session_synced=True`；
- 同步失败保留 unsynced checkpoint，后续 resume 可补同步；
- prune 只删除 terminal 且已同步的 Run，不删除 running/paused/unsynced Run。

## 4. Interaction Broker

Agent 保持同步。API 推荐让 Agent 工作线程阻塞在端口，并由事件桥线程取请求：

```python
import dataclasses

request = interaction.next_request(timeout=1.0)
if request is not None:
    payload = dataclasses.asdict(request)
    publish_interaction_request(payload)
```

请求共有：

- `kind`：`approval | question | continue | definition_change | recovery`；
- `request_id`、`run_id`，以及适用时的 `session_id`、`call_id`；
- 各类请求自己的合法选项和脱敏信息。

REST 响应到达后，API 按 `kind` 构造对应 decision：

```python
interaction.respond(ApprovalDecision(request_id, "allow"))
interaction.respond(QuestionAnswer(request_id, answer="方案 A", available=True))
interaction.respond(ContinueDecision(request_id, continue_run=False))
interaction.respond(DefinitionChangeDecision(request_id, accepted=True))
interaction.respond(RecoveryDecision(request_id, "skip"))
```

安全语义：

- 错误 request ID、错误 decision 类型、非法选项、过期或重复响应返回 `False`；
- timeout、端口关闭、端口异常全部拒绝授权/停止续跑/拒绝定义变化/abort recovery；
- `close()` 幂等并唤醒所有等待者；
- approval DTO 含 tool、capabilities、脱敏展示目标、risk、精确 scopes 和可选 broader scope；
- definition change 只给字段级差异和哈希，不给完整 system prompt/tool schema；
- recovery 明确给出重复副作用风险；
- Agent 侧不包含 HTTP、WebSocket、asyncio broker 或 FastAPI。

API 可用 `asyncio.to_thread()` 包装 `next_request()` 和 Agent worker，但不要在 Agent 公共层引入 async。

## 5. 事件契约

公共事件从 `assistant_agent.service` 导入。当前：

```python
EVENT_CONTRACT_VERSION == 1
```

关键规则：

- tool_call/tool_result 使用同一个 `call_id` 配对；
- 工具展示优先使用 `event.display`（`ToolDisplay`），不要在 API 重解析原始 arguments；
- `kind == "reasoning"` 时 `sensitive=True`，API 默认丢弃，不进入 Web DTO、日志或重连缓存；
- 每个公共 Run 流最后有且仅有一个 `kind == "run_terminal"`；
- `terminal_status` 明确为 `completed | failed | paused | cancelled`；
- 原有 final/error/interrupted 仍保留，兼容 CLI；网络层只以 `run_terminal` 判断 Run 终态；
- API 自己增加 seq、timestamp、session_id、run_id、heartbeat 和重连缓存；
- 新增可选字段保持兼容，破坏性变更会提升契约版本。

## 6. Runtime notice 与生命周期

`session_runtime.runtime.notices` 是结构化启动通知，字段为：

```python
RuntimeNotice(code, message, level, details)
```

包括未信任 Skill 跳过、MCP warning/auto-approve、容器外能力和上下文不足等。服务初始化不会等待
终端输入；未显式信任的 project/configured Skill 默认不注入模型。

关闭顺序由 Agent 负责：拒绝新 Run -> 请求取消 -> 关闭 InteractionPort 并唤醒等待 -> 关闭 MCP、
WebClient、Workspace/ProcessSupervisor -> 结束 logger。`close()` 和初始化失败回滚均幂等。API 仍负责
join 自己创建的工作线程。

推荐 API 生命周期：

1. 创建/载入 SessionRuntime；
2. 把 `execution.events` 放入单独工作线程持续消费；
3. pause/cancel 和 interaction response 从其他线程提交；
4. 收到 `run_terminal` 后结束本次 worker；
5. Session 保持活跃时复用 Runtime；淘汰 Session 时调用 `session_runtime.close()`；
6. 进程 shutdown 时先 close 所有 SessionRuntime，再 join API worker。

## 7. Agent 不负责的 API 职责

以下仍由 `assistant_agent_api` 实现：

- FastAPI Router、HTTP/WebSocket、认证、Origin/CORS；
- Web DTO、interaction request 的网络发布与响应路由；
- seq/timestamp/heartbeat、事件缓存和断线重连；
- Runtime Pool、并发配额、Session 淘汰和 worker join；
- 数据库、Redis、任务队列和多租户隔离；
- 默认过滤 sensitive reasoning。

## 8. Agent 侧验收证据

- Fake provider 公共门面端到端：`tests/test_agent_service.py`；
- 跨线程授权、错误 ID、重复响应、timeout、close：`tests/test_interaction_port.py`；
- 事件版本/敏感标记/向后兼容：`tests/test_service_contract.py`；
- Runtime 回滚、Skill 信任和幂等关闭：`tests/test_setup.py`；
- 分层约束：`tests/test_architecture.py`。

M16 完成基线：566 passed、5 skipped、覆盖率 83%，Ruff、mypy、架构适应度测试全绿；未修改
`src/assistant_agent/agent/loop.py`。
