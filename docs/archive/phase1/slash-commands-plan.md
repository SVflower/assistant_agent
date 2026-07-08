# Slash 命令系统方案 — 可发现的会话控制层

> 目标：把散落的 `/model`、exit 收编成体系，补上"用户不知道有哪些能力"的缺口。
> 对齐 Claude Code 的 `/` 命令：本地拦截、不进 ReAct、不花 token。
> 状态：待审阅，未动代码。基础档（纯打印、鲁棒、不依赖键盘监听）。
> 最后更新：2026-07-02

---

## 一、结论

做**基础档**：命令注册表 + `/help` 可发现 + 收编现有命令（/model、exit）+ 补高频元命令。
纯打印、任何终端都稳、**不动内核**。实时下拉菜单（prompt_toolkit）作为后续可选增强，本期不做
（依赖脆弱的键盘监听，我们踩过 Git Bash 失灵的坑）。

## 二、为什么（解决的问题）

- **可发现性**：用户不用记/查文档就知道有哪些命令（现状痛点：`/model` 用户根本不知道存在）。
- **元命令与任务分离**：`/` 前缀本地拦截、不发模型、不花 token；自然语言才进 ReAct。
- **降低记忆负担**：每条命令带一句说明。
- **体系化**：现在 `/model`、exit 硬编码散在 if 里 → 统一注册表管理。

## 三、设计（仿 ToolRegistry，不动内核）

### 命令抽象
```
SlashCommand: name, description, handler(args: str, ctx) -> None
SlashRegistry: register / get / list（供 /help 与分发）
```
chat 循环：输入以 `/` 开头 → SlashRegistry 分发；否则进 loop.run。本地处理、不进循环。

### handler 需要的上下文
命令要操作会话（切模型、清会话、看用量、退出），需访问 config/loop/console/session。
用一个轻量 `ChatContext`（持有这些引用 + 一个"是否退出"标志）传给 handler，避免 handler 直接依赖 main 的局部变量。

### 首批命令
| 命令 | 说明 | 行为 |
|------|------|------|
| `/help` | 列出所有命令 | 打印名字+说明表 |
| `/model [名]` | 切换模型 | 收编现有 _handle_model_command |
| `/sessions` | 列出历史会话 | 复用 SessionStore.list + console.print_sessions |
| `/clear` | 开新会话 | 新建 session、清空 loop 历史 |
| `/context` | 看 token/上下文用量 | 打印当前会话消息数、最近一轮用量（若有）|
| `/exit`（含 quit）| 退出 | 置退出标志 |

> 收编后，chat 循环里不再散落 `if task == "/model"...`，统一走注册表。

### UX（基础档）
- 输入 `/`（单独）或 `/help` → 打印命令列表（名字 + 说明），一眼看全。
- 未知 `/xxx` → 提示"未知命令：xxx，输入 /help 查看可用命令"。
- 纯 `self._console.print`，无 prompt_toolkit 依赖 → 任何终端稳。

## 四、涉及文件（不动内核）
| 文件 | 改动 |
|------|------|
| `agent/`? 否 | 内核不动 |
| 新增 `cli/commands.py`（或 `ui/`）| SlashCommand / SlashRegistry / 内置命令 handler |
| `main.py` | chat 循环：`/` 开头交注册表分发；构造 ChatContext |
| `ui/console.py` | 加 `print_commands(rows)`（若 console 逼近 300 行，放 formatting）|
| tests | 注册表分发、/help 列出、未知命令、/clear 清历史 |

**分层**：新命令模块只依赖 session/config/ui（不反向依赖 agent 循环），置于 main 之下。
注意架构测试与 console 300 行预算（必要时把渲染放 formatting）。

## 五、开发计划（每步带测试）
1. SlashCommand + SlashRegistry + ChatContext（新模块）→ 单测：注册/查找/未知命令
2. 内置命令 handler（help/model/sessions/clear/context/exit）→ 单测（用假 ctx）
3. main chat 循环接线：`/` 分发、收编现有 /model 与 exit
4. `/help` 与未知命令的打印
5. DoD：pytest + ruff + 架构测试全绿；README/CLAUDE 命令说明同步

## 六、验收标准
1. chat 里 `/help`（或 `/`）列出所有命令 + 说明
2. `/model`、`/sessions`、`/clear`、`/context`、`/exit` 各自生效
3. 未知 `/xxx` 给友好提示，不崩、不进 ReAct
4. slash 命令本地处理、不发模型（不产生 LLM 调用）
5. 现有测试不回退；新增命令测试；ruff + 架构测试通过；内核未动

## 七、范围与边界
- ❌ 不做实时筛选下拉菜单（prompt_toolkit，脆弱）——后续可选增强，带降级。
- ❌ 不做自定义命令模板 / 命令即 skill（属更上层 skill 体系，未到）。
- ⚠️ slash 命令一律本地拦截，绝不把 `/xxx` 当任务发给模型。
- ⚠️ 收编 /model 时保持原行为（菜单选/直切/保留历史）不回退。
