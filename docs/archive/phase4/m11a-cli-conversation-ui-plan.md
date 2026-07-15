# M11a 计划：CLI 对话展示重构

> 状态：已完成；按用户确认实施，未修改 `agent/loop.py`。
> 目标：把当前“原始执行轨迹”升级为默认简洁、需要时可审计、对本地小模型仍稳健的 Agent CLI。

## 1. 背景与问题

当前执行与恢复能力已经成熟，但展示层仍直接消费通用 `StepEvent` 的原始字段：

- `tool_call` 把参数拼成 `k=v`；多行正文会突破“一行截断”，文件内容、绝对路径和潜在密钥进入
  终端 scrollback。
- `tool_result` 固定预览 500 字符，随后模型往往再次复述，造成正文、工具结果、最终说明三重重复。
- `content_delta` 以纯文本逐片打印，模型输出的标题、列表、代码块和分隔线保留为裸 Markdown。
- 正常成功任务也显示完整 Run ID；启动面板和过程元数据抢占主要视觉层级。
- Console 同时负责状态机、spinner、Markdown、工具展示和交互，已超过 300 行软线，继续叠逻辑会扩大
  D11。
- 只有 `show_reasoning` 开关，缺少“正常使用 / 自动化 / 排障审计”的展示层级。

这不是改变 Agent 决策能力，而是建立独立的 presentation contract：模型和工具继续获得完整数据，用户
默认只看到完成任务所需的信息。

## 2. 成熟产品调研

### 2.1 Claude Code

- [交互模式](https://code.claude.com/docs/en/interactive-mode) 默认折叠工具细节，`Ctrl+O` 打开带时间与
  模型信息的完整 transcript；MCP 的重复调用可聚合成一行。
- [Fullscreen rendering](https://code.claude.com/docs/en/fullscreen) 支持点击展开工具结果，并单独解决
  长对话重绘、闪烁和内存问题。

借鉴：默认轨迹和审计轨迹分层；错误与等待用户输入必须突出。暂不照搬 fullscreen、鼠标和 alternate
screen，它们需要完整 TUI 状态管理，不适合本期 Rich scrollback 架构。

### 2.2 OpenAI Codex CLI

- 公开 CLI 提供 quiet 非交互模式，只输出最终结果；常规轨迹把文件探索、命令执行和计划更新压成动作
  摘要，而不是打印函数 JSON。
- [长命令实时输出需求](https://github.com/openai/codex/issues/4751) 说明单一 spinner 对分钟级命令
  不够，但无界输出同样会淹没会话。

借鉴：自动化输出与交互输出分开；长动作至少显示当前动作和持续时间。实时 PTY/stdout 留到独立里程碑，
不借 UI 改造绕开 M10c 的取消/进程监管边界。

### 2.3 Gemini CLI

- [`ToolMessage`](https://github.com/google-gemini/gemini-cli/blob/d2cd12a7/packages/cli/src/ui/components/messages/ToolMessage.tsx)
  将工具状态、描述、结果和展开状态结构化处理。
- [ToolDisplay 迁移](https://github.com/google-gemini/gemini-cli/pull/25186) 把 `name`、`description`、
  `resultSummary`、`result` 交给工具生成，支持 dense/full 两种视图并修复摘要重复。

借鉴：工具拥有 UI 无关的语义展示数据，UI 决定密度；不要在 renderer 里堆按工具名分支。

### 2.4 Aider

- [`MarkdownStream`](https://github.com/Aider-AI/aider/blob/bdb4d9ff/aider/mdstream.py) 用 Rich Markdown +
  Live 渐进渲染，提交稳定行并只重绘尾部窗口，兼顾 Markdown 与流式反馈。

借鉴：Markdown 流式渲染应是独立组件并测试碎片边界；不复制其代码，只采用“稳定前缀 + 活动尾部”原则。

## 3. 决策原则

1. **默认展示意图，不展示协议**：显示“读取 notes.txt”，不显示
   `read_file(path=D:\\...\\notes.txt)`。
2. **可审计但不默认倾倒**：normal 是默认；verbose 显示脱敏详情；quiet 只输出最终结果或错误。
3. **工具定义语义，UI 定义样式**：工具层产生纯数据，不 import Rich/UI。
4. **脱敏是最终出口硬要求**：无论工具自定义摘要还是 fallback，进入终端前统一递归脱敏、转义控制字符、
   限长。
5. **错误优先于简洁**：失败、拒绝、预算耗尽、恢复不确定状态在 normal 下自动展开必要诊断。
6. **保留 scrollback 兼容**：本期不切 alternate screen，不依赖鼠标，不破坏管道和 dumb terminal。

## 4. 范围

### 4.1 必做

1. 新增 UI 无关 `ToolDisplay` 契约，至少包含动作、目标、结果摘要、可选详情和状态。
2. BaseTool 提供默认展示方法；内置 list/read/write/edit/search/shell/git/ask/skill 覆盖语义摘要；MCP 和
   未知扩展工具走安全 fallback，不要求第三方立即实现。
3. `StepEvent` 携带结构化展示字段；`execution.py` 从 Registry 取得 call/result display。
4. 新增 `normal`、`verbose`、`quiet` 三种模式：配置 `ui.display_mode`；chat 支持
   `/display [normal|verbose|quiet]` 即时切换；`run --quiet` 覆盖为 quiet。
5. normal：每个工具通常占一行，执行中显示动作+耗时，完成后原地或紧邻更新状态；错误最多展开限定行数。
6. verbose：显示工具注册名、短 call ID、脱敏参数、返回 code/metadata 和有界结果预览。
7. quiet：成功仅输出最终回答；错误输出稳定错误文本；确认交互和安全警告不能被隐藏。
8. 独立 `StreamingMarkdownRenderer`：支持标题、列表、行内代码、代码块；流式碎片不泄露 Rich markup，
   完成后 transcript 中不残留 Markdown 控制符。
9. Run ID 正常成功时不单独占行；verbose 始终可见，暂停/失败/需恢复时在任何模式显示完整 ID 和命令。
10. 启动 banner 压缩信息层级，但权限模式和“无 OS 沙箱”声明必须保留。
11. 调整系统提示词：过程说明限一句普通文本，不用标题/分隔线/emoji 编号；最终回答只报告改动、验证和
    遗留问题。该约束只减展示噪音，不改变工具调用与安全协议。
12. 为 60/100/160 列、Windows/Linux、TTY/非 TTY 建立确定性 renderer 测试。

### 4.2 可选（有余量再做）

- 同一轮连续同类只读工具聚合为 `读取 4 个文件`，但必须能在 verbose 看见每个调用。
- normal 模式下对长 Shell 结果显示最近 3 行滚动预览；前提是不改变当前进程捕获正确性。

### 4.3 本期不做

- fullscreen/alternate-screen TUI、固定底部输入框、鼠标点击展开。
- 后台任务、PTY、Shell 真正实时 stdout/stderr、跨平台进程树取消。
- 并行工具调用、async Loop、改变 checkpoint 或权限语义。
- 修改 `agent/loop.py` 控制流。

## 5. 技术设计

### 5.1 展示契约

新增 `tools/display.py`：

```python
@dataclass(frozen=True)
class ToolDisplay:
    action: str
    target: str = ""
    summary: str = ""
    detail: str = ""
```

BaseTool 提供 call/result 的默认实现；具体工具只返回纯文本/数字，不返回颜色、图标或 Rich renderable。
Registry 负责查找工具并调用展示方法，fallback 参数先经 `sanitize_for_display()`，字符串中的换行、制表和
控制字符转义后再限长。

`ToolResult.output` 仍完整进入模型上下文，`ToolDisplay` 只服务 UI。不得为了“界面简洁”截断模型实际
收到的工具观察。

### 5.2 事件流

扩展 `agent/events.py` 的 `StepEvent`，增加 `call_id`、`display`、`result_code`、`result_metadata` 等可选
字段。`agent/execution.py` 在既有 `tool_call/tool_result` 事件上填充，不新增循环状态，不改变 M10b
checkpoint 顺序。

### 5.3 Renderer 拆分

- `ui/conversation_renderer.py`：消费 StepEvent、管理模式和任务统计。
- `ui/tool_renderer.py`：normal/verbose 工具活动、宽度适配和错误展开。
- `ui/markdown_stream.py`：流式 Markdown 稳定前缀与活动尾部。
- `ui/console.py`：保留输入、确认、菜单和对上述 renderer 的编排。
- `ui/formatting.py`：保留无状态表格/数字格式化，删除原始 `format_args()` 职责。

这样能让 `console.py` 回到 300 行软线以下；若 Markdown Live 在特定终端不可用，自动降级为当前纯文本
流式路径，不能因美化阻塞回答。

### 5.4 模式行为

| 信息 | normal（默认） | verbose | quiet |
|------|----------------|---------|-------|
| 模型过程文本 | 临时 Markdown 活动区，工具调用后清除 | 完整 Markdown 并保留 | 隐藏 |
| 工具调用 | 语义动作+目标 | 工具名+call ID+脱敏参数 | 隐藏 |
| 成功结果 | 一行摘要 | 摘要+有界详情/metadata | 隐藏 |
| 错误/拒绝 | 自动展开必要诊断 | 完整有界诊断 | 显示 |
| Run ID | 失败/暂停时显示 | 始终显示 | 失败/暂停时显示 |
| 最终回答 | Markdown | Markdown | 纯文本稳定输出 |
| token/耗时 | 完整单行 footer | 完整单行 footer | 默认隐藏 |

## 6. 是否动内核

**不修改 `agent/loop.py`。** 事件 DTO 与工具批次执行器会扩展，但循环终止、预算、权限、恢复和历史写回
均不变。若实现中发现必须改 Loop，立即停止并按铁律重新向用户确认。

## 7. 测试计划

1. `ToolDisplay`：各内置工具的动作/目标/结果摘要；未知/MCP fallback。
2. 安全：嵌套 key/token/password、常见 secret 值、ANSI escape、换行/制表在所有模式均不泄露或注入。
3. Markdown：每字符碎片、围栏跨 chunk、未闭合反引号、中文宽字符、超长单词、空内容、异常降级。
4. 模式：normal/verbose/quiet 的事件矩阵；错误、拒绝、notice、interrupted 和恢复提示永不被误隐藏。
5. 终端：60/100/160 列快照；非 TTY、`NO_COLOR`、Windows UTF-8 与 emoji 缺字时不破版。
6. Slash/CLI：`/display` 参数、无参状态、错误选项；`run --quiet`；配置迁移默认 normal。
7. 回归：现有 StepEvent 构造保持兼容；M10b recovery eval 和 18 个 scripted eval 轨迹不变。

## 8. 验收标准

以用户提供的 CRUD 场景作为固定展示验收：

1. normal 中每次工具调用不超过 2 行；不出现 `content=`、`old_string=`、完整文件正文或重复 workspace
   绝对路径。
2. read/write/edit 分别显示“读取 N 行”“写入 N 字符/N 行”“替换 N 处”等真实摘要；错误不伪装成功。
3. 最终输出正确渲染标题、列表、代码块，不出现裸 `###`、`**`、`---` 或 emoji 缺字方框。
4. 工具结果正文不会与模型最终说明重复刷屏；verbose 仍可查看有界、脱敏的原始详情。
5. 任意模式下测试密钥都不出现在终端文本；ANSI 控制序列不能改变终端状态。
6. 成功 Run 不单独显示完整 ID；暂停/失败给出可直接执行的 resume 命令。
7. `pytest --cov`、架构测试、Ruff、mypy、scripted 18/18、recovery 4/4 全绿。

## 9. 实施顺序

1. P1：ToolDisplay 契约、fallback 脱敏、内置工具摘要和单元测试。
2. P2：事件字段与 renderer 拆分，先交付 normal/verbose 工具活动。
3. P3：StreamingMarkdownRenderer 与终端宽度/降级测试。
4. P4：quiet、`/display`、`run --quiet`、Run ID/banner/提示词收敛。
5. P5：CRUD 真实截图验收、全量 DoD、状态文档、计划归档和提交。

## 10. 风险与边界

- **流式 Markdown 闪烁**：限制 Live 为 15 FPS，normal 使用 transient 活动区；异常时无损降级纯文本。
- **摘要掩盖错误**：只压缩成功结果；错误、拒绝和不确定恢复状态保留诊断与 code。
- **第三方工具无摘要**：安全 fallback 始终可用，ToolDisplay 是可选增强而非注册门槛。
- **模式造成测试组合膨胀**：纯 renderer 输入固定事件序列做参数化矩阵，不依赖真实模型。
- **UI 反向污染工具层**：工具只产 DTO，不出现 Rich、颜色、终端宽度或交互逻辑。

## 11. 审阅门

用户确认本方案后才实施。方案默认允许修改 `agent/events.py`、`agent/execution.py`、`tools/`、`ui/`、
`cli/commands.py`、`config/schema.py`、`main.py` 和提示词；**不包含修改 `agent/loop.py` 的授权**。

## 12. 实施结果

- ToolDisplay、三种展示模式、`/display`、`run --quiet`、流式 Markdown、Run ID/banner 和提示词收敛
  全部落地。
- CRUD normal 轨迹实测不显示原始写入/替换正文和重复绝对路径；verbose 有界且递归脱敏；quiet 只输出
  最终回答。
- `console.py` 从 303 行降至 207 行；展示状态机、工具活动和 Markdown 已按职责拆分。
- 根据真实 CLI 反馈追加低密度修正：normal 启动区保留紧凑结构化面板，以标题和“模型/位置/权限”
  三行恢复视觉层级，同时隐藏面板外的新会话 ID；工具间模型旁白只在 transient Live 活动区显示，
  不进入终端历史；最终回答建立独立边界；授权风险去重；token 级强制刷新改为 15 FPS 节流。
- 411 passed、2 skipped，覆盖率 80%；Ruff、mypy、架构测试、18/18 scripted eval、4/4 recovery
  eval 全绿。

## 13. Normal 信息分级复审（2026-07-15）

根据真实 CRUD 使用反馈和 Claude Code 的写入/编辑轨迹，原“每个成功工具压成一行”的规则修正为
“按信息价值分级”，而不是对所有工具使用相同密度：

| 工具类别 | 调用前 | 成功后 | 失败时 |
|----------|--------|--------|--------|
| Read/List/Search | spinner 中显示动作，不落历史 | 一行语义摘要 | 摘要 + 有界诊断 |
| Shell/Git/MCP | spinner 中显示动作，不落历史 | 一行退出/结果摘要 | 摘要 + 有界诊断 |
| Write | 文件名 + 有界代码预览 | 一行写入统计 | 摘要 + 有界诊断 |
| Edit/MultiEdit | 文件名 + 有界拟议 diff | 一行替换统计 | 摘要 + 有界诊断 |

写操作是例外，因为内容本身就是用户做授权和结果审阅所需的信息。预览由 `ToolDisplay` 提供纯数据，
normal renderer 决定终端样式；所有内容先脱敏、去控制字符，再按字符数和行数双重截断。预览在
`tool_call` 事件到达时展示，时序早于 Registry 权限确认，因此无需改变权限策略、checkpoint 或 Loop。

本次仍不实现 `Ctrl+O` 展开、固定底栏或可点击折叠；这些能力需要保存完整渲染状态的 TUI，不能在 Rich
scrollback 架构中用表面样式冒充。

复审验收：418 passed、2 skipped，覆盖率 80%；Ruff、mypy、18/18 scripted eval、4/4 recovery eval
全绿；7342 行生产 Python 源码 + 1366 行 eval 基础设施；`agent/loop.py` 无改动。

后续真实 CLI 回归修正：移除输入横线 40 列硬上限，任务回显和聊天输入均使用终端全宽上下边界；normal
恢复新建/续接会话 ID，退出时读取当前 `ctx.session.id`，即使中途 `/clear` 也准确报告实际结束的会话。
真实 TTY 通过 prompt_toolkit `bottom_toolbar` 在编辑期间保持下边界，提交后再落稳定 scrollback 边界；
非 TTY 保留内置 input 回退。最终验收 420 passed、2 skipped，覆盖率 82%；7363 行生产 Python 源码，
Loop 仍无改动。

代码区视觉复审：Write 预览统一使用无边框深灰代码底板；Edit/MultiEdit 的上下文行沿用代码底色，
新增/删除内容改为整行绿/红背景，文件头与 hunk 弱化显示，不再只用高饱和前景色表达修改。40/100
列均验证背景覆盖整行。最终验收 421 passed、2 skipped，覆盖率 82%；7396 行生产 Python 源码。

quiet 回归修正：过程性 `Console.info()` 继续受 quiet 抑制，新增不受展示模式影响的
`Console.command_info()`；所有 slash 命令反馈统一走控制面出口。真实 chat 序列
`/display quiet` → `/display` → `/help` → `/display normal` 已覆盖。最终验收 423 passed、2 skipped，
覆盖率 82%；7405 行生产 Python 源码，Loop 无改动。
