# M15 CLI 活性反馈与关键动作可见性方案

> 状态：已完成并归档（2026-07-17）。本方案未修改 `agent/loop.py`，不展示或持久化模型隐藏推理。

## 1. 问题与目标

当前 CLI 已在模型空窗和工具调用期间使用临时 dots spinner，但存在三个实际缺口：

1. 授权提示会停止 Live，用户允许后只打印静态“执行中…”，慢 Shell、MCP、写入看起来像卡死；
2. spinner 文本里的耗时只在阶段切换时计算一次，不会随等待持续增长；
3. normal 模式多数工具意图只在临时活动区出现，关键副作用缺少稳定、简洁的“准备做什么”标识。
4. 模型输出一句工具前旁白后，可能用数秒到数十秒生成大文件内容等工具参数；完整调用组装前没有
   `tool_call` 事件，流式 Markdown 停在静态文字上，看起来像程序卡死。

补充实测反馈：当前等待动画会出现卡顿。代码层原因是 spinner 耗时只在 `spin()` 被调用时计算，
事件间等待不会更新；reasoning、Markdown、工具和确认之间反复 stop/start 两套 Rich Live；Windows
Terminal 在 12/15 FPS 全量重绘下更容易出现刷新抖动。同步第三方代码若长时间持有 GIL，也会短暂
阻塞 Rich 自动刷新线程。

M15 的目标是建立一致的 CLI 活性反馈和关键动作轨迹，让用户随时知道 Agent 正处于哪个阶段、
正在等待谁、如何中断，同时不把原始 chain-of-thought 当作 UI 内容。这里的“活性”是指用户能
持续确认进程仍在工作，不代表虚构完成百分比或提前宣称副作用已经发生。

## 2. 设计原则

- **单一活动焦点**：同一时刻最多一个动画，避免多个 spinner 抢占终端；
- **阶段而非内心独白**：展示“分析任务 / 等待授权 / 编辑文件 / 等待 MCP”等可验证状态，不展示
  隐藏推理；已有 `ui.show_reasoning` 仅在用户显式开启时维持原行为；
- **瞬时状态不堆积**：等待动画使用 transient Live；完成后由现有工具结果行替换；
- **关键动作可追溯**：写入、编辑、Shell、扩展安装和 MCP 写操作在执行前落一行简短意图；普通读取
  和搜索只保留临时活动，避免 normal 模式变回日志瀑布；
- **等待可控制**：长等待显示 `Ctrl+C 可暂停`，第二次 Ctrl+C 强制取消仍沿用 M14 语义；
- **展示模式一致**：normal 显示关键状态，verbose 显示全部阶段，quiet 不增加过程输出；
- **可访问与可降级**：TTY 使用动画；非 TTY、`TERM=dumb` 或低动态配置使用静态文本，不输出
  控制序列。连续动画只用于真实等待，不作装饰。

参考 Claude/Codex 类 CLI 的“当前动作 + 目标 + 完成结果”结构，以及通用 UX 原则：超过约 300ms
的等待应有反馈、多步骤任务要有阶段状态、避免同时运行多个持续动画。

## 3. 技术设计

### 3.1 ActivityController

新增 `ui/activity.py`，由 Console 持有一个控制器：

- `show(action, target)`：开始或原地切换活动；
- `suspend()`：授权/用户输入前停止动画并保留当前上下文；
- `resume(action=None)`：用户允许后恢复动画；
- `complete()`：工具结果、最终回答、错误或中断时幂等停止；
- 动态 renderable 每次刷新重新计算阶段耗时，不再显示冻结的 `0.0s`；
- 每个任务只创建一次 Activity Live，阶段切换使用原地 update，不因 reasoning 碎片反复 stop/start；
- Activity 默认使用 8 FPS，减少 Windows Terminal 无收益重绘；
- 等待超过 8 秒时追加低干扰提示 `仍在等待 · Ctrl+C 可暂停`；
- Console/Renderer 异常退出时 finally 清理 Live，不遗留活动行。

ActivityController 只依赖 Rich 和 UI 类型，不反向依赖 agent/tools。

流式 Markdown Live 同时做空闲检测：最后一个正文碎片到达 1 秒后仍无新事件时，在正文下方临时
显示 `模型仍在生成 · <耗时>`，超过 8 秒追加暂停提示。新正文到达后立即隐藏并重置；完整工具调用
到达后切换到准确的工具活动状态。该提示不读取、不展示工具参数碎片，因此不会泄露待写文件内容。

### 3.2 活性状态机

ConversationRenderer 使用现有 StepEvent，不新增 Loop 事件：

| 区间 | 用户可见状态 | 结束条件 |
|---|---|---|
| 请求发出到首个事件 | `等待模型响应` + 动态耗时 | reasoning、正文、工具调用或终止事件到达 |
| reasoning（默认隐藏） | `分析任务` + 动态耗时 | 正文、工具调用或终止事件到达 |
| 正文持续到达 | 流式 Markdown 本身 | 正文停更或其他事件到达 |
| 正文停更但模型流未结束 | 1 秒后显示 `模型仍在生成` + 空闲耗时 | 新正文、完整工具调用或终止事件到达 |
| 完整工具调用到达 | `正在<动作> <目标>`；关键副作用另落稳定意图 | 工具结果到达 |
| 等待用户授权/澄清 | 明确的确认框或问题菜单，不运行等待动画 | 用户提交、取消或中断 |
| 用户允许授权 | `执行已授权操作` + 动态耗时 | 工具结果到达 |
| 用户允许继续更多轮次 | `继续处理` + 动态耗时 | 下一模型事件到达 |
| 工具结果到下一轮 | `评估下一步` + 动态耗时 | 下一模型事件到达 |
| notice 后继续 | `继续处理` + 动态耗时 | 下一事件到达 |
| final/error/interrupted/异常 | 幂等清理所有 Live | 回合结束 |

完整工具调用后的动作名称继续由 `ToolDisplay` 映射：read/list 为读取/查看，write/edit 为写入/编辑，
search/web 为搜索/获取，shell/git 为运行/检查，MCP 与 Skill/扩展使用各自语义名称。目标继续使用
现有脱敏和长度限制，不直接渲染原始参数。

无法从现有事件可靠区分“网络暂时无包”和“模型正在生成工具参数”时，统一使用“模型仍在生成”，
不猜测具体内部阶段。直接工具调用且尚无正文时继续显示“等待模型响应”，保证有活性反馈但不误导。

### 3.3 关键动作摘要

在 `tools/display.py` 增加 UI 无关的重要性字段或判定函数：

- `routine`：读取、列目录、只读 Git、搜索；只显示临时活动；
- `change`：write/edit/multi_edit；沿用代码预览，并把标题统一为“准备写入/准备修改”；
- `external`：Shell、MCP、Skill/MCP 配置；normal 在执行前落一行 `◆ 准备…`；
- 工具完成继续使用现有 `✓/x + 语义摘要`，不重复完整参数。

这不是模型推理摘要，而是根据已经发出的结构化工具调用生成的可审计动作说明。

### 3.4 授权与输入协调

- `confirm` / `confirm_scoped` / `ask_question` 进入交互前调用 `suspend()`；
- 用户允许后调用 `resume("执行已授权操作")`，直到对应 tool_result 才停止；
- 拒绝后不恢复动画，等待 tool_result 展示拒绝结果；
- `confirm_continue` 同意后恢复 `继续处理`，覆盖下一轮模型首包前的空窗；拒绝后保持停止；
- prompt_toolkit/questionary 与 Rich Live 不同时占用终端；
- 取消、异常和 Runtime 退出全部幂等清理。

## 4. 范围

### 必做

- 动态计时的统一 ActivityController；
- 流式正文后的模型空闲反馈，覆盖长工具参数生成和网络停顿；
- 模型、文件、Shell、Git、Web、MCP、Skill 阶段文案；
- 授权后恢复动画；
- 关键副作用意图的稳定摘要；
- normal/verbose/quiet 与非 TTY 降级；
- 单元测试和 renderer 集成测试。

### 可选

- `ui.motion: auto | reduced` 显式配置。若 Rich/终端能力已能可靠判断，本期可只做自动降级；
- 8 秒长等待阈值后续可配置，本期先保留内部常量，避免配置膨胀。

### 不做

- 不让模型额外调用一次来总结自己的推理，避免增加 token、延迟和幻觉；
- 不默认展示原始 reasoning/chain-of-thought；
- 不实现百分比进度，模型和任意工具没有可信总量，伪进度会误导；
- 不修改 Loop、工具执行协议、权限策略或 checkpoint；
- 不增加全屏 TUI、任务队列或多 Agent 时间线。

## 5. 文件边界

- 新增 `src/assistant_agent/ui/activity.py`；
- 修改 `ui/conversation_renderer.py`、`ui/console.py`、`ui/tool_renderer.py`；
- 按需小改 `tools/display.py` 的展示契约；
- 新增/扩展 `tests/test_cli_activity.py`、`tests/test_cli_display.py`；
- 更新 README、ROADMAP、TECH_DEBT 和当前状态，完成后归档至 `docs/archive/phase8/`。

## 6. 测试计划

- fake clock 验证动态耗时与 8 秒长等待提示；
- 慢事件生成器验证等待期间计时持续增长，Activity Live 每任务只 start 一次；
- fake clock 验证流式正文空闲 1 秒后出现反馈、新正文到达后重置、8 秒后显示暂停提示；
- 高频 reasoning 碎片只更新状态，不重复创建/销毁 Live；
- start/switch/suspend/resume/complete 幂等，任何时刻只有一个 Live；
- 授权允许后动画恢复，拒绝后不恢复；
- write/edit/Shell/MCP 的阶段文案脱敏、截断且目标正确；
- normal 只落关键动作，verbose 落全部，quiet 无过程输出；
- tool_result/error/interrupted/final/finally 均清理活动状态；
- 最大轮数确认同意后恢复、拒绝后不恢复；
- 非 TTY/低动态模式不产生控制序列；
- 原有流式 Markdown、确认输入、代码预览和 token 状态条不回退；
- 全量 `pytest --cov`、Ruff format/check、mypy、架构适应度测试全绿。

## 7. 验收标准

1. 写入、编辑、Shell、MCP 等操作等待期间持续有动作名、目标和实时耗时；
2. 模拟 2 秒无事件等待时，计时和 spinner 仍持续刷新且无 Live 生命周期抖动；
3. 权限放行后到工具完成之间不再出现无动画空窗；
4. 模型生成大文件工具参数期间不再停在静态旁白，超过 8 秒明确提示 Ctrl+C 暂停方式；
5. 重要副作用执行前有稳定摘要，最终结果仍只输出一次；
6. normal 不堆积普通读取轨迹，quiet 行为不回退；
7. 不展示隐藏推理，不新增模型调用或 token 消耗；
8. 不修改 `agent/loop.py`，全量质量门通过。
9. 从请求开始到回合结束，除用户正在输入和不足 1 秒的短间隔外，不出现无法解释的静态空窗。

## 8. 风险边界

- Rich Live 与 prompt_toolkit 冲突：所有输入前强制 suspend，finally 兜底；
- 快工具闪烁：保持单行 transient，不落屏；若真实体验仍明显，再引入延迟显示，不先上后台线程；
- “重要”判定不准确：只按工具能力类别判定，不分析自然语言内容，不把猜测包装成事实；
- Windows 终端差异：动画仅使用 Rich 内置 spinner 和 ASCII/现有符号，非 TTY 自动静态降级；
- GIL 边界：纯 Python/C 扩展若长时间不释放 GIL，任何同进程刷新线程都可能短暂停顿；本期不为
  动画流畅度把任意工具迁移到后台线程，避免破坏 M14 的取消、checkpoint 和副作用语义；
- 原始 reasoning：继续受 `ui.show_reasoning` 显式开关控制，不把它当作默认关键决策展示。

## 9. 实施结果

- 新增单 Live `ActivityController`，以 8 FPS 动态渲染当前阶段耗时；阶段连续等待超过 8 秒时提示
  `Ctrl+C 可暂停`，阶段变化会重置等待计时；
- 流式 Markdown 在正文停更 1 秒后显示动态“模型仍在生成”，解决长工具参数尚未组装成
  `tool_call` 时的静态空窗；新正文到达即重置，不展示参数内容；
- 模型连接、隐藏 reasoning、工具调用、工具结果、notice、授权恢复和终止路径已接入统一活动状态；
- normal 仅持久展示写入/编辑预览与 Shell、MCP、扩展管理等外部动作，普通读取和搜索不堆积；
- quiet、异常清理、notice 恢复、普通/分级授权以及 Live 复用均有回归测试；
- 未修改 `agent/loop.py`，未新增技术债；全量质量门为 545 passed、5 skipped、覆盖率 83%，
  Ruff format/check 与 mypy 全绿；
- 用户完成真实 Windows Terminal 反馈后补齐长工具参数生成空窗；提交前审查修复流式缓冲重复处理、
  计时并发快照和 `TERM=dumb` 降级问题，方案随后归档。
