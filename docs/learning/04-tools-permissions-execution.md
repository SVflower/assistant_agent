# 04 工具、权限与执行环境

## Tool 的最小组成

一个工具通常提供：

- `name` 与 `description`：给模型理解能力。
- `parameters`：JSON Schema，约束参数。
- `permission_requests()`：根据具体参数声明能力和目标。
- `run()`：执行并返回结构化 `ToolResult`。
- `display_call/result()`：生成脱敏的人类展示。

工具不直接打印终端内容，也不自行绕过 Registry 申请权限。

## Registry 是统一安全漏斗

所有内置工具、声明式 Python 工具和 MCP 工具最终都进入 `ToolRegistry.execute()`：

```text
按名查找
  -> JSON Schema 参数校验
  -> 计算 PermissionRequest
  -> pre-tool observers
  -> 权限策略 / Interaction
  -> 工具调用预算
  -> tool_started checkpoint
  -> Tool.run
  -> 单次与累计输出限制
  -> post-tool observers / 审计
  -> tool_completed checkpoint
```

这个顺序非常重要。参数校验和权限必须发生在副作用前；预算计数不能被失败重试绕开；工具异常统一为
`ToolResult`，不能把原始异常、环境变量或密钥抛给模型/API。

## ToolContext 是一次 Runtime 的依赖包

`ToolContext` 给工具提供 Workspace、RunControl、logger、ArtifactStore、权限策略和预算。工具通过它访问
运行环境，而不是读取全局变量。这样两个 Session Runtime 的授权记忆、进程和产物不会互相污染。

`current_call_id` 用于把工具审计、Artifact 和 checkpoint 绑定到同一次模型工具调用。

## 权限模型

工具把动作描述为 capability + target，例如文件读取、文件修改、进程执行、网络访问、MCP 调用。策略按
`deny -> ask -> allow` 判断。拒绝或 Interaction 超时都 fail closed，绝不能把“没有回答”当作允许。

权限不是 sandbox：应用层允许命令后，宿主进程仍拥有当前系统用户的 OS 权限。真正的执行边界由
Workspace 决定：

- `off`：宿主兼容模式。
- `workspace`：路径/cwd 限制在项目内，但不是 OS 隔离。
- `container`：Shell/Git 在受限容器运行，默认无网络、非 root。

Web Runtime 还有更强的注册白名单：危险工具根本不进入 schema，因此模型无法通过参数“切回 CLI”。

## ReplayPolicy 与副作用不确定

只读且可证明安全的调用可以标记 `safe_readonly`。写文件、外部 API 写操作等一般是
`requires_decision`。恢复时如果只保存到 `started`：

- safe readonly 可以按规则重试；
- 未知副作用必须询问 retry/skip/abort；
- timeout、断线和 non-interactive 均不自动 retry。

## 输出为什么要限制两次

来源端应限制 stdout/stderr 或网络响应，避免进程先吃光内存；Registry 还执行统一的单次和累计字符限制，
避免工具结果挤爆模型上下文。大输出可落入受管 Artifact，模型和服务只拿 opaque ID。

## 如何新增只读工具

推荐从一个纯函数式工具开始：

1. 在 `tools/` 新建实现，继承 `Tool`。
2. 写严格 JSON Schema，拒绝多余字段。
3. 声明精确 `PermissionRequest`，不要用一个宽泛目标覆盖所有参数。
4. 返回稳定 `code`、`retryable` 和脱敏 metadata。
5. 在 `bootstrap/tools.py` 按 RuntimePolicy 注册。
6. 测试合法参数、非法参数、拒绝、预算、异常和展示脱敏。

如果是用户自己的业务系统，优先放在 `D:\Dev\mcp\<server>`，Agent 仓库只承担通用 MCP 接入。

## 关键源码

- `tools/tool.py`：Tool 基类。
- `tools/registry.py`：统一执行链。
- `tools/context.py`：运行依赖与授权入口。
- `tools/permissions.py`：能力和请求 DTO。
- `execution/workspace.py`：工作区边界。
- `execution/process.py`：deadline、PIPE 排空和进程树清理。

