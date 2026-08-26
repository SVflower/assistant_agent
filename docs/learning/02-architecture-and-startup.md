# 02 架构与启动过程

## 为什么要分层

如果 Agent Loop 直接创建 LiteLLM、打开 JSON 文件、打印 Rich UI 并启动 MCP，它将很难测试，也无法被
API 复用。本项目采用 Ports & Adapters：核心描述自己需要的能力，边缘模块提供具体实现，`bootstrap`
负责把它们连接起来。

| 层 | 负责 | 不负责 |
|---|---|---|
| `contracts` | 公共 DTO、事件、失败和 Interaction 协议 | 业务编排、IO |
| `agent` | ReAct、上下文、Run 状态转换 | CLI、HTTP、具体数据库 |
| `application` | Session/Run 用例与生命周期 | 创建具体 LiteLLM/MCP 对象 |
| `bootstrap` | 唯一对象装配点 | UI 展示和状态机规则 |
| `service` | 稳定导出 | 复制实现逻辑 |
| adapters | Provider、Store、Workspace、MCP 等具体能力 | 反向控制核心策略 |

## CLI 启动链

```text
python -m assistant_agent
  -> assistant_agent/__main__.py
  -> assistant_agent/main.py 的 Typer command
  -> cli/setup.py:build_runtime
  -> bootstrap/runtime.py:create_runtime
  -> application/runtime.py:AgentRuntime
  -> AgentLoop.run / SessionRuntime.start_run
```

`__main__.py` 只转发到 `main()`。`main.py` 读取命令参数和渲染事件，但不应自己组装模型/MCP。CLI adapter
把 `ConsoleInteractionAdapter` 传入公共工厂，使权限询问通过终端完成。

`cli/setup.py` 负责 CLI 特有的事情：寻找默认配置、把类型化启动错误显示给用户、打印 banner。真正可
复用的装配全部在 `bootstrap.create_runtime`，因此服务端不会依赖 Typer、Rich 或终端输入。

## Service 启动链

```python
from assistant_agent.service import AgentService, RuntimePolicy

service = AgentService(
    config_path=config_path,
    workspace_root=workspace_root,
    runtime_policy=RuntimePolicy.web(),
)
session = service.create_session(interaction=my_port, interactive=True)
execution = session.start_run("总结这个主题")
for event in execution.events:
    publish(event)
```

调用顺序：

```text
service.AgentService (公开入口)
  -> bootstrap.service.AgentService (注入本地 adapter)
  -> application.sessions.AgentService (Session 用例)
  -> SessionRuntime (绑定一个 Session 的 Run 用例)
```

API 只消费公共对象与 `ItemEvent`，不应导入 `cli`、解析终端文本或复制 checkpoint 状态机。

## create_runtime 为什么长

`create_runtime` 是 composition root。它需要创建配置、Workspace、日志、Skill、WebClient、MCP、工具
Registry、Provider、Store、AgentLoop 等对象，所以代码天然会比普通函数长。它的高内聚点不是“这些
对象功能相似”，而是“它们必须以确定顺序创建，并在失败时逆序关闭”。

大致阶段：

1. 加载并校验配置和 RuntimePolicy。
2. 创建 RunControl、进程监管和 ToolRegistry。
3. 准备 Workspace 与日志。
4. 发现受信 Skill，构造有界 system prompt。
5. 创建工具上下文，按 policy 注册内置工具。
6. 启动 Web 和 MCP；optional MCP 失败只形成 notice。
7. 创建 LLM client、Conversation、AgentLoop 和 Store。
8. 返回拥有全部资源的 `AgentRuntime`。

如果第 6 步失败，前五步创建的资源也必须关闭。工厂不打印错误，而是抛类型化异常；展示层决定如何
告诉用户。

## RuntimePolicy 与 config 的区别

- `config.yaml` 是部署配置，例如模型、预算、sandbox、已配置的 MCP。
- `RuntimePolicy` 是可信调用方施加的能力上界，例如 Web profile 根本不注册 Shell 和服务器文件工具。

模型能看到工具 schema，但不能修改 policy。即使配置里启用了某工具，policy 不允许时它仍不可见。
CLI 使用 `RuntimePolicy.cli()`；服务器面向浏览器时使用 `RuntimePolicy.web()`。

## 生命周期

`AgentRuntime.close()` 必须幂等：调用两次不会重复破坏资源。关闭时还要唤醒等待中的 Interaction，并
默认拒绝，停止受管进程、MCP/Web 客户端、Workspace 和日志。创建失败和正常关闭遵循同一资源责任。

## 继续阅读

- 装配：[runtime.py](../../src/assistant_agent/bootstrap/runtime.py)
- CLI adapter：[setup.py](../../src/assistant_agent/cli/setup.py)
- 公共 Service：[service/__init__.py](../../src/assistant_agent/service/__init__.py)
- Application 用例：[sessions.py](../../src/assistant_agent/application/sessions.py)

