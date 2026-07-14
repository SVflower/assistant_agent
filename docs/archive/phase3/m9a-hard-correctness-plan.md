# M9a 计划：硬正确性与工程基线

> 状态：已完成，2026-07-14。上位规划见 `docs/phase3-trustworthy-agent-plan.md`。
> 用户已于 2026-07-14 指示完成第三阶段规划，并授权按既定边界推进本里程碑。

## 1. 目标

把第二阶段审计中已经复现的正确性问题变成自动化硬保证：任何模型请求不突破配置窗口，
Session ID 不能逃出存储目录，持久化中断不破坏旧会话，Runtime/MCP 构造失败不遗留资源，
模型切换后主调用、默认摘要器和会话元数据保持一致。

## 2. 范围

### 必做

1. 新增独立 token 估算接口与最终上下文封套校验；固定开销本身超过窗口时在配置阶段拒绝。
2. 最新用户消息无法装入最小请求时给稳定错误，不把超限请求交给 provider。
3. 摘要结果限制长度，写入 checkpoint 后再次受最终封套约束；损坏 checkpoint 安全拒绝。
4. Session ID 使用稳定格式校验和目录 confinement；保存使用同目录临时文件加 `os.replace()`。
5. MCP 单 server 连接失败时关闭该连接已打开资源；Runtime 构造失败时逆序清理 logger/MCP。
6. `AgentLoop.set_client()` 更新跟随当前模型的默认 Compactor；固定 `summary_model` 保持不变。
7. `/model` 同步 Session provider/model，并记录模型切换事件；恢复语义固定为“沿用当前配置”。
8. 校验 `summary_model` 必须存在；上下文窗口必须能容纳 system、tools schema、回复预留和至少一条消息。
9. CI 增加 Python 3.11/3.13、pytest、coverage、format、lint 和类型检查；限制 MCP 主版本 `<2`。

### 不做

- 不实现 M9b 权限策略或 OS 沙箱。
- 不实现 M10b 步骤级运行 checkpoint。
- 不重写 ReAct 控制流，不改变工具预算和终止协议。
- 不承诺所有 provider 的精确 tokenizer；估算失败时使用保守字符估算。

## 3. 技术设计

### 3.1 ContextEnvelope

- `agent/token_budget.py` 提供 `TokenEstimator` 协议、保守实现和消息/schema 计数。
- `Conversation.messages()` 是最终封套出口，返回前验证 `used <= max_context_tokens`。
- 截断按完整消息保留；最新消息也不能例外。若本轮用户输入本身无法装入，抛出领域错误，
  Loop 转成稳定 `error` 事件并终止，不调用 LLM。
- checkpoint 载入校验字段类型、游标范围和摘要长度；摘要输出按 `summary_max_tokens` 保守裁剪。

### 3.2 Session 安全

- Session ID 仅允许项目生成的时间戳加随机后缀格式；所有入口统一校验。
- 路径 resolve 后必须仍是 sessions 目录的直接子文件。
- JSON 先写同目录临时文件，flush + fsync 后 `os.replace()`；异常时删除临时文件，旧文件保留。

### 3.3 生命周期与模型切换

- MCP `_connect_one()` 使用连接私有 `AsyncExitStack`，完整初始化成功后才移交 manager 总 stack。
- `build_runtime()` 用局部资源和 `try/except` 回滚，避免构造到一半泄漏。
- Loop 记录 Compactor 是固定 provider 还是跟随主 client；`set_client()` 只更新后者。
- `/model` 更新当前 Session 元数据；日志增加向后兼容的 `model_switch` 事件。

## 4. 内核边界

允许轻触 `agent/loop.py`：注入 estimator、捕获封套错误、同步默认 Compactor client。
不改变循环分支、工具执行顺序、重复熔断或预算语义。该边界已随第三阶段执行指令获得授权。

## 5. 测试计划

- Context：超大最新消息、超长摘要、固定开销超限、坏 checkpoint、正常兼容路径。
- Session：`../`、绝对路径、分隔符、非法 ID、原子替换失败保留旧文件。
- Loop：超限不调用 client；默认/固定摘要器切换语义。
- Config：无效 `summary_model`、不可用窗口组合。
- MCP/Runtime：transport、initialize、注册或 Runtime 后续构造失败均清理已开资源。
- Commands/Obs：`/model` 同步 Session 元数据并产生审计事件。

## 6. 验收标准

1. 所有模型调用前的最终封套估算不超过配置窗口。
2. 审计中四个已复现问题均有失败优先测试并转绿。
3. `pytest`、`pytest --cov`、`ruff format --check .`、`ruff check .`、类型检查全绿。
4. Windows/Linux CI 配置覆盖 Python 3.11 与 3.13，依赖不允许 MCP 2.x 漂移。
5. D13、D15、D17 按实测结果更新；状态文档与 ROADMAP 同步，计划归档到 `docs/archive/phase3/`。
