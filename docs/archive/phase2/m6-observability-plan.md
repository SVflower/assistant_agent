# M6 — 结构化日志与工具审计（第二阶段第一项）

> 状态：已完成
> 归属阶段：第二阶段「可观测、可扩展、可接生态」的第一块地基
> 最后更新：2026-07-08

## 解决什么问题

当前 agent 每一步（调了什么工具、参数是什么、耗时多久、成功还是失败、危险操作被允许还是拒绝）
只在终端流式一闪而过，**跑完不留痕**。这带来三个缺口：

1. **不可观测**：任务出错后无法回看"它究竟做了哪几步、哪一步崩的"。
2. **不可审计**：危险操作（删除/覆盖/shell/区外写）的授权决策没有留痕，谈不上"权限与审计"。
3. **挡住后续**：多 Agent、真沙箱、生态接入这些复杂能力，调试都依赖"能看清每步在干嘛"——没有日志地基，它们会很痛。

这是第一阶段就欠下的债（"无结构化日志"），也是第二阶段所有复杂能力的前置。

## 调研（借鉴原则，不照搬）

- **结构化日志优于纯文本**：业界共识是事件按机器可读格式落盘（JSON Lines：一行一个 JSON 事件），
  便于事后 grep / 用脚本聚合，而不是拼人类可读字符串。（通用工程实践，高置信）
- **可观测与审计是一条流的两个视角**：审计事件（谁在何时授权了什么危险操作）本质是结构化事件的一个子集，
  没必要一开始就拆成两套系统——先用统一事件流 + `type` 字段区分，需要时再抽审计视图。（设计判断）
- **日志不进 UI 主通道**：日志落文件，默认不污染终端流式输出；终端仍靠既有的 ItemEvent 渲染。（对齐"状态可见性"与"日志分离"）
- **本地优先、隐私优先**：日志只落本地、随 `.assistant_agent/` 一起 gitignore；参数/输出可能含敏感信息，
  需截断 + 尽力脱敏，且提供开关。（本项目"绝不提交密钥"铁律的延伸）
- 不确定项：各闭源产品（Claude Code / Codex）内部日志的确切 schema 未公开，本方案不臆测其字段，只借鉴"结构化 + 事件化"这一原则。

## 架构评估（是否动内核）

**不动内核 `agent/loop.py`。** 已核对代码，落点全在内核之外：

| 落点 | 位置 | 记录什么 |
|------|------|---------|
| 工具调用 | `tools/registry.py` `ToolRegistry.execute`（第 44-52 行） | 工具名、参数（脱敏截断）、耗时、成功/失败、输出长度 |
| 权限审计 | `tools/base.py` `ToolContext.request_confirm`（第 42-54 行） | 类别、决策（allow/always/deny）、是否命中"永久允许"记忆 |
| 会话生命周期 | `main.py` `_setup` / `run` / `chat` | 会话开始（provider/model/模式/cwd）、任务文本、会话结束 |

关键洞察：`ToolContext` 是 `registry.execute` 与 `request_confirm` **唯一都能触到**的对象
（execute 收 `ctx` 参数，request_confirm 是 ctx 的方法）。把 logger 作为 ToolContext 的一个注入字段，
两个落点都能用 `ctx.logger.xxx()` 写事件，且都在 tools 层，**内核零改动**。

**新增 `obs/` 层，rank 0**：logger 是底层基础设施，tools/agent/main 都要用它，所以必须在最低层
（与 config/session 同级）。会在架构测试 `_LAYER_RANK` 里登记 `obs: 0`，让护栏**主动强制**
"obs 不许 import tools/agent/ui"——这是强化护栏，不是放宽。

## 范围

### 必做（M6 核心）
1. **`obs/logger.py`（新模块，rank 0）**：
   - `EventLogger`：把事件按 JSONL 追加到日志文件；方法 `tool_call / confirm / session_start / session_end / task`。
     每条事件含 `ts`（ISO 时间）、`session_id`、`type`、以及该事件的字段。
   - `NullLogger`：无操作实现，作为 `ToolContext.logger` 的默认值——不配置/关闭时零副作用，测试也用它。
   - 脱敏 + 截断工具函数：参数/输出超长截断（配置 `max_payload_chars`）；对明显密钥模式
     （`sk-...` 等）与疑似敏感键名（`key/token/password/secret`）的值做遮蔽。
   - 单写者、主线程内追加，无需锁（Ctrl+C 只置标志、不写日志，工具执行在主线程）。
2. **配置 `LoggingConfig`**（`config/schema.py` 新增，挂到 `AppConfig.logging`）：
   - `enabled`（默认 `true`）、`dir`（默认 `.assistant_agent/logs`）、
     `log_tool_io`（默认 `true`：是否记录参数/输出载荷，关掉则只记元数据）、
     `max_payload_chars`（默认如 2000）。
   - 文件名按天分卷：`logs/YYYY-MM-DD.jsonl`（简单有界，易于清理）。
3. **接入两个落点**：`registry.execute` 加计时 + `ctx.logger.tool_call(...)`；
   `request_confirm` 加 `ctx.logger.confirm(...)`。
4. **`ToolContext` 加 `logger` 字段**（默认 `NullLogger()`），`main._setup` 构建真正的 `EventLogger` 注入，
   并记录 `session_start`；`run`/`chat` 记录 `task` 与 `session_end`。
5. **架构测试登记 `obs: 0`** + 相应单测。

### 可选（本期不做，视时间）
- `/audit` slash 命令：从日志里筛出危险操作决策，人读友好展示。
- 把 ItemEvent 的 final/error/interrupted 也落日志（需在 ui/main 消费处接一钩子）。

### 明确不做
- 日志轮转/保留策略（除按天分卷外的自动清理）——记为未来信号（日志体积变大时再做）。
- 记录完整 LLM prompt/response 载荷——隐私与体积风险，延后。
- 把日志发往任何外部服务/远端——违背本地优先，不做。
- 独立的第二套审计存储/数据库——统一事件流已够，YAGNI。

## 技术设计（草案，细节实现时定稿）

事件示例（JSONL，一行一条）：

```json
{"ts":"2026-07-08T15:40:12.331","session_id":"20260708-...","type":"tool_call","tool":"shell","args":{"command":"echo hi"},"duration_ms":42,"status":"ok","output_len":3}
{"ts":"2026-07-08T15:40:10.000","session_id":"20260708-...","type":"confirm","category":"run_shell","decision":"allow","remembered":false}
{"ts":"2026-07-08T15:40:09.900","session_id":"20260708-...","type":"session_start","provider":"deepseek","model":"...","mode":"chat","cwd":"d:/..."}
```

- `EventLogger.__init__(dir, session_id, log_tool_io, max_payload_chars)`：确保目录存在，持有当天文件路径。
- 写入失败**非致命**（对齐现有"自动保存失败只警告"原则）：吞掉异常，绝不因日志写不了而中断任务。
- 脱敏：`_redact(value)` 对字符串按正则遮蔽密钥；`_truncate(s)` 超长截断加省略标记。
- `session_id` 复用会话存储已有的 id 语义（run 模式无持久会话时用启动时间戳生成一个临时 id）。

## 测试计划

- `tests/test_obs.py`（新）：
  - JSONL 格式正确、每行可 `json.loads`、含必需字段。
  - `NullLogger` 全方法无操作、不建文件。
  - 脱敏：`sk-xxxx`、`password` 键值被遮蔽；截断在 `max_payload_chars` 生效。
  - 写入失败（如目录不可写）不抛异常。
- `tests/test_tools.py` / `test_architecture.py` 补充：
  - `registry.execute` 在注入 logger 时产生一条 `tool_call` 事件、含 `duration_ms` 与 `status`。
  - `request_confirm` 三种决策各产生一条 `confirm` 事件。
  - 架构测试 `_LAYER_RANK` 含 `obs: 0`，且 `obs/` 不 import 更高层（复用现有 no_upward 规则）。
- `tests/test_config.py` 补充：`LoggingConfig` 默认值与覆盖解析。

## 验收标准

1. 跑一次带工具调用的任务后，`.assistant_agent/logs/<日期>.jsonl` 生成，含该次的
   `session_start` + 各 `tool_call`（带耗时/状态）事件，每行可被 `json.loads`。
2. 一次危险操作（如区外写/危险 shell）的授权决策在日志里留痕（`confirm` 事件，含 allow/always/deny）。
3. 日志中不出现明文密钥：脱敏回归测试通过（构造含 `sk-`/`password` 的参数，断言被遮蔽）。
4. `logging.enabled=false` 时不写任何文件、行为与现在一致（NullLogger 路径）。
5. 日志写入失败不影响任务完成（非致命）。
6. **内核 `agent/loop.py` 未改动**；架构测试（含新增 `obs: 0` 规则）+ 全量测试全绿，ruff 全绿。
7. 新增测试覆盖：obs logger、两个落点事件、配置解析。

## 风险与边界

- **脱敏是尽力而为，非保证**：无法覆盖所有密钥形态。缓解：日志仅本地、随 `.assistant_agent/` gitignore
  不入库；提供 `log_tool_io=false` 一键只记元数据。文档需明确告知这一限度。
- **日志体积**：JSONL 只增不减。M6 用按天分卷把单文件控制在合理范围；自动清理留作未来信号，不在本期。
- **性能**：每次工具调用多一次文件 append + JSON 序列化，开销可忽略（工具本身耗时远大于此）。
- **破铁律 4？否**：不动内核。落点全在 tools 层与 main 层，`obs/` 为新的底层扩展点。
- **DoD 对齐**：完成前跑 `pytest`（含架构测试）+ `ruff` 全绿；新增关键路径测试；本方案文档 + 债册更新入库；提交前审查 `git diff --cached` 无密钥/垃圾。
