# ask_user 实施方案 — 层1 澄清工具

> 目标：把 M2.5 设计的"层1 模型层澄清"从"提示词驱动的自然语言提问（chat 才有、要结束当轮）"
> 升级为**结构化、循环内、不打断**的工具能力。参考 Claude Code 的 AskUserQuestion。
> 状态：待审阅，未动代码。
> 最后更新：2026-07-02

---

## 一、为什么做（补全两层设计）

M2.5 定了两层用户选择：
- 层1 **意图澄清**（要什么/怎么做）——当时只做了"提示词让模型用自然语言问"，缺陷：
  chat 才有、且模型问完要**结束当轮**等下条消息，循环被打断。
- 层2 **权限确认**（准不准执行危险操作）——已做成 `ToolContext.request_confirm`。

`ask_user` 把层1 升级为**一等工具**：模型调用 → 阻塞等用户选 → 选择作为工具结果喂回 →
**同一轮继续执行**。好处：循环不中断、选项结构化、可跨 run/chat（见决策1）。

与层2 的关系：**机制同源（都要停 spinner、读输入），语义不同，并存分工**。
- `ask_user` = 澄清意图，无副作用，不算危险操作。
- `confirm` = 授权危险操作。

## 二、两个关键决策（含推荐）

**决策1：run 模式能否提问？→ 用 isatty() 判定，而非按 run/chat 一刀切**
- 已实测：真实终端 `sys.stdin.isatty()=True`，管道/自动化=False。
- 方案：**有终端就允许问；无终端（管道/无 tty）时工具直接返回"当前无人可应答，请基于最合理假设自行决定"**，模型据此退回假设（与"run 遇歧义自行假设"一致）。
- 这比"run 一律不问"更准：run 在真实终端里其实也能问；只有真正非交互时才退回假设。

**决策2：和自然语言澄清的取舍 → ask_user 优先，自然语言兜底**
- 提示词引导：需要用户定夺时**优先调 `ask_user`**；纯粹的开放式追问仍可自然语言。
- 不删除自然语言能力，只是把"有明确选项的抉择"导向结构化工具。

## 三、技术设计（复用现有注入模式，内核不动）

### 数据流（完全复刻 confirm 的注入模式）
```
Console.ask_question(question, options) —— 停 spinner、列选项、读输入、返回所选
        ↓ 注入
ToolContext.ask —— 与 confirm 并列的回调
        ↓ 被调用
AskUserTool（tools/ask.py）—— 调 ctx.ask(...)，把用户选择作为 ToolResult 返回
        ↓ 注册
build_default_registry —— 模型可调用
```

### 工具 schema
```
name: ask_user
description: 当需求有歧义或有多个合理方案需用户定夺时，向用户提问并列出选项，
             等用户选择后再继续。这是澄清意图，不是执行授权。
input:
  question: string              # 必填，要问的问题
  options: string[]             # 必填，2~5 个候选项（供用户选择）
output (ToolResult.ok):
  "用户选择：<所选项原文>"        # 交互式：用户选的选项
  或用户自定义输入（选“其他”时的自由文本）
  非交互（无 tty）：ok("当前非交互环境，无用户应答；请基于最合理假设继续并说明假设。")
```

### Console.ask_question(question, options) -> str
- 复用 confirm 的三件事：① 停掉活动 spinner（`self._active_live`）② 若非行首先补换行
  ③ Rich 渲染问题 + 带编号的选项。
- 读输入：用户输编号选选项；额外提供"其他"→ 允许自由文本输入。
- 返回用户选择的**选项原文**（或自由文本）。

### ToolContext 变更
- 新增字段 `ask: Callable[[str, list[str]], str]`，默认返回"无人应答"退化串（安全默认）。
- main 的 `_setup` 注入 `console.ask_question`（与现有 `confirm=console.confirm` 并列）。
- AskUserTool 内部先判 `sys.stdin.isatty()`：非交互直接返回退化串，不调 ask。

### 权限
- `ask_user` 是**只读式澄清**，无副作用 → **不走 confirm、不算危险操作**。
- 唯一防滥用：无 tty 时不阻塞（直接退化），避免自动化场景卡死。

### 流式事件展示
- 走现有 `tool_call` / `tool_result` 事件，无需新事件类型。
- 提问 UI 由 Console.ask_question 直接渲染（同 confirm，spinner 已停）。

## 四、错误处理
- 缺 question/options，或 options 为空 → `ToolResult.error(清晰说明)`。
- 用户直接回车/EOF → 视为"未选择"，返回提示让模型自行决定（不崩）。
- registry.execute 已兜底任何异常。

## 五、涉及文件
| 文件 | 改动 | 动内核？ |
|------|------|:---:|
| `tools/ask.py` | 新增 AskUserTool | 否（新工具） |
| `tools/base.py` | ToolContext 加 `ask` 字段 | 否 |
| `tools/registry.py` | 注册 AskUserTool | 否 |
| `ui/console.py` | 加 `ask_question` 方法 | 否 |
| `main.py` | `_setup` 注入 `ask=console.ask_question` | 否 |
| `agent/prompts.py` | 引导优先用 ask_user 澄清 | 否 |
| `tests/test_ask.py` | 新增测试 | — |

内核 `agent/loop.py` **不动**。

## 六、开发计划（每步带测试）
1. `ToolContext.ask` 字段 + `AskUserTool`（tools/ask.py）+ 注册 → 单测（注入假 ask 回调）
2. `Console.ask_question` 方法（停 spinner、列选项、读输入、"其他"自由文本）
3. `main._setup` 注入；提示词引导优先用 ask_user
4. 非交互降级：无 tty 时返回退化串（用 monkeypatch 模拟 isatty=False 测）
5. DoD：pytest + ruff + 架构测试全绿（ask 工具在 tools 层，不反向依赖 agent/ui）

## 七、验收标准
1. 交互式下 `ask_user` 弹出问题+编号选项，用户选择被喂回、循环继续（不结束当轮）
2. 非交互（管道/无 tty）下 `ask_user` 不阻塞，返回退化串，模型退回自行假设
3. `ask_user` 不触发危险确认（它是澄清，非授权）
4. 缺参/空选项被拒；空回车不崩
5. 新工具带测试；现有测试不回退；ruff + 架构测试通过；内核未动

## 八、范围与边界
- ❌ 不做多轮嵌套问卷、不做 GUI 选择器——一次一问，编号选择足够。
- ❌ 不让它变成"绕过 run 非交互"的后门——无 tty 一律退化，不阻塞自动化。
- ⚠️ 提问不等于授权：ask_user 只澄清意图，任何危险执行仍必须走层2 confirm。
- 参考来源：Claude Code AskUserQuestion（结构化多选澄清）的设计理念；不照搬其 UI 细节。
