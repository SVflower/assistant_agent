# 07 调试方法与练习

## 先判断问题在哪一层

| 现象 | 先看 |
|---|---|
| 启动慢或失败 | Runtime startup event、MCP capability、`bootstrap/runtime.py` |
| 模型没有调用工具 | system prompt、注册后的 tool schemas、provider 流式解析 |
| 工具明明存在却拒绝 | PermissionRequest、RuntimePolicy、Interaction response |
| 一直运行 | Run snapshot 的 phase/budget、命令 deadline、provider timeout |
| Web 状态与 checkpoint 不一致 | `run_terminal`、`session_synced`、Application 事件收口 |
| 恢复后重复执行 | tool call status、ReplayPolicy、`tool_uncertain` |
| 会话内容不对 | Session ledger；不要只看压缩后的 Conversation |

## 三份事实怎么查

1. **StepEvent**：调用方当时看到了什么，事件顺序是否正确。
2. **Run checkpoint**：任务为何停在某个 phase，预算和工具状态是什么。
3. **Session ledger**：最终公开历史是什么，assistant 回复关联哪个 user。

日志用于诊断和审计，但不是权威状态。API 不应通过搜索中文日志决定 Run 状态。

## 用测试学习调用链

```powershell
# 找某个类的测试
rg "SessionRuntime|ToolRegistry" tests

# 跑一个文件并显示测试名
pytest tests/test_service.py -vv

# 失败时停在第一个错误
pytest tests/test_service.py -x -vv

# 查看哪些行没有覆盖
pytest tests/test_service.py --cov=assistant_agent --cov-report=term-missing
```

测试名可能随里程碑变化，先用 `rg --files tests | rg "service|run|tool"` 找当前文件。

## 使用调试器

在想观察的位置写临时 breakpoint（不要提交）：

```python
breakpoint()
```

然后运行定向测试。常用命令：`n` 下一行，`s` 进入函数，`p value` 打印，`c` 继续，`q` 退出。
对于 generator，断点会在消费 `next()` 时触发。

不要在并发/MCP 卡顿问题里随意加大量 `print`；它可能改变线程时序。优先读取结构化 activity、timeout、
checkpoint 和受管日志。

## 练习 1：跟踪一次 Fake Provider Run

目标：画出实际事件顺序。

1. 找到使用 Fake/Scripted provider 的 Loop 测试。
2. 在测试里记录每个 `event.kind`、`call_id` 和 `terminal_status`。
3. 验证 tool_call/tool_result 的 call_id 配对。
4. 验证完整正文 `final` 在唯一 `run_terminal(completed)` 之前。

## 练习 2：新增纯只读工具

实现一个返回当前项目 Python 文件数量的工具，但不要调用 shell：使用 Workspace/Path 能力。要求：

- 参数 schema 禁止多余字段。
- 路径只能在 workspace 内。
- 声明 filesystem read 权限。
- 输出有界，异常返回稳定 ToolResult。
- 测试正常、越界、拒绝、非法参数和预算耗尽。

完成后思考：它应该在 Web profile 可见吗？“只读”是否自动意味着不泄露服务器信息？

## 练习 3：故障注入

在 Fake Store 中让某次 checkpoint 写入失败，验证：

- 上一个有效槽仍可读取。
- 没有把未确认的工具标为 completed。
- 事件最终是一个 failed terminal。
- Session 不会记录不存在的完整 assistant 回复。

## 练习 4：理解 MCP 桥

从 `MCPManager` 构造开始，标出：event loop 在哪个线程创建、同步调用如何提交 coroutine、timeout 在
哪里生效、close 如何唤醒等待者。然后解释为什么不能直接在 Agent Loop 中到处调用 `asyncio.run()`。

## 练习 5：从 Service 嵌入 Agent

写一个仅在本地运行的小脚本：创建 `AgentService`、创建 Session、消费 StepEvent、打印唯一 terminal，
最后通过 context manager/`close()` 释放资源。不要解析 `event.text` 来判断失败，使用
`terminal_status` 和结构化 `failure`。

正式接入前再阅读 [Agent Service 集成契约](../agent-service-integration-guide.md)，本手册用于理解，契约
文档才是调用方实现的事实源。

## 完成标准

当你能不看目录回答以下问题，就已经掌握主干：

1. CLI 与 API 在哪里汇合？
2. 为什么 final 不能表示 Run 完成？
3. 为什么工具执行前要保存 started checkpoint？
4. Conversation、Session、RunState 各保存什么？
5. optional MCP 失败为什么不阻止 Agent 启动？
6. RuntimePolicy 与权限配置、sandbox 分别解决什么问题？

