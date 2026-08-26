# M19 Agent API 架构交接

> 目标项目：`assistant_agent_api` 及后续任何 Python 进程内调用方。
> Agent 分支：`codex/m19-architecture-reconstruction`。
> 最终代码 commit：`a0d1bbeba224940993d2860b4f9ee0695d93360a`；API 应固定该 commit
> 或之后构建的 wheel。

## 1. 结论

M19 是内部架构重建，不要求 API 复制或重写状态机。公共 Service 根入口、ItemEvent v1、Run
checkpoint v3、Session/Run 状态、Interaction request/decision、failure code、权限、预算 continuation、
tool_uncertain 和 `session_synced` 语义保持兼容。

API 已只从 `assistant_agent.service` / `assistant_agent.interaction` 导入时，没有阻断性修改。
M19 新增稳定 DTO 所有权 `assistant_agent.contracts`，建议 API 渐进迁移 DTO/Protocol 导入并补契约测试。

M19 最终清理删除了全部迁移前内部包和转发文件，不提供 deprecation 过渡期。这是开发阶段的
Python import 破坏性变更，但不改变事件契约和运行语义。

## 2. API 必做项

1. 将 Agent 包固定到包含 M19 的 commit/wheel，不从源码目录拼接 `PYTHONPATH`。
2. 启动时继续断言 `EVENT_CONTRACT_VERSION == 1`。
3. 确认 API 未导入 `assistant_agent.agent`、`application`、`bootstrap`、`providers`、`tools`、
   `execution`、`persistence`、`observability`、`integrations`、`cli` 或 `ui` 内部模块。
4. 保持每 Session 一个隔离 `SessionRuntime`、同 Session 同时最多一个 Run、Iterator 在线程中消费。
5. 保持 `final` 不是终态、`run_terminal` 唯一终态、failed 读取结构化 failure 的现有映射。
6. 跑完整 API pytest、Ruff、mypy 和 Agent/API 联调测试。
7. 将 API `pyproject.toml` 中固定的 Agent commit 从 M18 `0ef635a...` 更新到本次 M19 最终提交。
8. 将 API `tests/test_runtime_adapter.py` 的
   `from assistant_agent.tools.display import ToolPreview` 改为
   `from assistant_agent.contracts import ToolPreview`；当前生产代码未发现其他内部路径导入。

以下旧路径已不存在，API/Web 代码、测试和 monkeypatch 目标必须一并删除或迁移：

| 已删除路径 | 唯一目标所有权 |
|---|---|
| `assistant_agent.llm` | `providers/`；服务调用方不得直接使用 |
| `assistant_agent.mcp/skills/web` | `integrations/`；服务调用方通过 Runtime 能力使用 |
| `assistant_agent.runtime` | `execution/`；服务调用方通过 `service` 使用 Runtime |
| `assistant_agent.session` | `persistence/`；服务调用方通过 `AgentService` 使用 Session |
| `assistant_agent.obs` | `observability/`；服务调用方不得读取内部日志实现 |
| `agent.events/failures/run_state/recovery/...` | `contracts/` 或 `agent/run/`；调用方只允许 `contracts` |
| `tools.base/result/artifacts/file_ops/process` | `tools` 目标模块或 adapter；调用方不得穿透 |
| `service.runtime/sessions/events/...` | `assistant_agent.service` 根入口 |
| `interaction.models/ports` | `assistant_agent.contracts` 或 `assistant_agent.interaction` 根入口 |

## 3. 建议修改

新代码推荐按所有权导入：

```python
from assistant_agent.contracts import (
    EVENT_CONTRACT_VERSION,
    InteractionPort,
    RunFailure,
    ItemEvent,
)
from assistant_agent.interaction import BlockingInteractionPort, SafeDefaultInteractionPort
from assistant_agent.service import AgentService, RuntimePolicy
```

已有 root import 可保留。不要改成从 M19 新内部目录导入具体 Store、RunCoordinator、AgentLoop、
MCPManager 或 provider adapter。

`RunExecution` 向后兼容新增：

```python
warning: str = ""
```

API 可把非空 warning 经自身脱敏后映射为诊断 notice，不应把原字符串直接发送到网络；它不是
ItemEvent、failure 或 terminal status，不能改变 Run snapshot 终态。旧代码不读取该字段时行为不变。

## 4. 明确无需修改

- WebSocket event kind、seq、timestamp、heartbeat 和重连缓存；
- ItemEvent DTO 字段或 `EVENT_CONTRACT_VERSION`；
- checkpoint schema 或数据库迁移；
- failure code / allowed_actions 映射；
- continuation、授权、ask_user、定义变化和 tool_uncertain Interaction DTO；
- pause/cancel/resume、原 run_id、`session_synced` 与 prune 规则；
- FastAPI/async 边界和 Runtime Pool 策略。

## 5. 联调验收

1. create Session -> start Run -> `final` -> `activity(syncing_session)` -> 唯一
   `run_terminal(completed)`。
2. Provider/预算失败 -> 唯一 `run_terminal(failed)`，API 按 `failure.code` 映射，不解析文本。
3. pause/cancel/resume 保持原 run_id；恢复定义变化和 uncertain tool 仍经 InteractionPort。
4. BlockingInteractionPort 错误 request ID、重复响应、timeout/close 均不授权。
5. 两个 SessionRuntime 的 Conversation、RunControl、授权、MCP 和 Workspace 相互隔离。
6. Runtime 初始化失败和 close 后无 MCP、WebClient、受管进程或等待线程残留。
7. 非空 `RunExecution.warning` 经 API 脱敏后只生成 notice，不改变 Run 状态。
8. API 测试中禁止内部目录 import，并固定检查事件契约版本。

## 6. Agent 验收证据

- pytest：604 passed / 5 skipped；
- coverage：84%；
- Ruff format/check：通过；
- mypy：通过；
- import-linter：12/12 contracts kept；
- scripted eval：18/18 PASS，27 tool calls，tokens 120/31；
- recovery eval：4/4 PASS；
- 生产 Python：13,467 行，120 个文件；eval 基础设施：1,404 行；
- 超过 600 行生产模块：0；
- ItemEvent：contract v1；Run checkpoint：schema v3。

长期契约以 `docs/agent-service-integration-guide.md` 为准；本文件是 M19 完成时的交接快照。
