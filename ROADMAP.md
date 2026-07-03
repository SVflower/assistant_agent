# ROADMAP — 后续路线图

> 开工蓝本。第一阶段（MVP）已完成，见 [DESIGN.md](DESIGN.md) 第 8 节。
> 本文档规划第二阶段起的里程碑，每个里程碑只列可清晰验收的目标。
> 最后更新：2026-07-01

---

## 第一阶段回顾（已完成 ✅）

**能力**：模型后端可切换（云端/本地）、ReAct 多轮工具调用、四工具（读/写/列/shell）、
环境感知提示词、CLI + 终端 UI。28 测试绿、私有仓库备份。

**当前边界**：无跨会话记忆、非流式（黑屏等待）、无初始化向导、单 Agent、非真沙箱、工具集窄。

**已查清的关键事实**：
- ✅ **同会话（短期）记忆已存在**：`chat` 复用同一个 `AgentLoop`/`Conversation`，`run` 追加不重置，连续对话能记得前文。
- ❌ **跨会话（中期）记忆缺失**：`chat` 退出后内存中的对话即丢失，下次重启从零开始。← 里程碑 3 的真正缺口。

**技术债**：上下文截断是"消息数量"而非"token"截断（本地小模型可能仍超窗）；本地模型工具调用不稳；无结构化日志。

---

## 里程碑总览与优先级

| 里程碑 | 主题 | 优先级 | 是否动内核 |
|--------|------|--------|-----------|
| M2 | 流式与过程透明 | P0 | 是（loop.py）| ✅ 已完成 |
| M2.5 | 任务中确认机制升级 + 中断 | P1 | 否（工具/UI 层）|
| M3 | 记忆与会话持久化 | P1 | 否（context + 存储层）|
| M4 | 工具集扩展 | P1–P2 | 否（tools/ 扩展点）|
| M5 | 上手体验（init 向导）| P2 | 否（加 CLI 子命令）|
| 延后 | 子 Agent 编排 / 完整沙箱 / Web GUI / rewind / recap | P3 | — |

**推进顺序**：M2 ✅ → M2.5 → M3 → M4 → M5。先解决最痛的体验（流式），再做人在环中的确认机制，再补最大能力缺口（记忆）。

---

## 设计哲学：Loop Engineering（贯穿所有里程碑）

> 参考 2026 年业界共识（Addy Osmani、Claude Code / Codex 作者）：agent 的质量不取决于
> 单次提示词，而取决于「观察→推理→行动→再观察」这个**循环**设计得好不好——
> "Build the Loop, Not the Prompt"。

这不是一个里程碑，而是评判每个里程碑的工程视角。我们的循环设计围绕六个关注点，
后续所有开发都应对照检查：

| 关注点 | 现状 | 归属 |
|--------|------|------|
| 循环终止（防跑飞/死循环）| ✅ `max_iterations` | 已有 |
| 上下文管理（不爆窗、留对的东西）| ⚠️ 按消息数截断 → 升级为 token 感知 | M3 |
| 错误恢复（工具失败循环不崩）| ✅ 工具异常归一为 ToolResult | 已有 |
| 反馈质量（喂回模型的观察够好）| ✅ 基本具备 | 已有 |
| 状态可见性（能看到循环在干嘛）| ❌ → 流式 + 分阶段状态 | M2 |
| 成本 / token 可见与控制 | ❌ → token 用量显示 | M2 |

> `agent/loop.py` 是稳定内核、`tools/` 与 `llm/` 是隔离扩展点——这套结构本身就是
> Loop Engineering 的落地。加能力优先走扩展点，不动内核（铁律 4）。

---

## M2 — 流式与过程透明（P0）✅ 已完成

**交付**：LLMClient 流式（碎片拼接实测 DeepSeek 后实现）、AgentLoop 消费流式（动内核，控制流不变）、
Console 流式渲染（Live spinner 与正文时间错开）、show_reasoning 开关、token 用量 + 任务计时显示。
35 测试全绿，云端 DeepSeek 实测流式通过。收尾时从 Claude Code 截图学习补充了计时（`耗时 Xs`）
与 token 方向（`↑输入 ↓输出`）显示。

**解决**：发问后长时间"黑屏"，思考/等待过程不透明。

**范围**：
- 流式 token 输出（`litellm` `stream=True`）
- 思考（reasoning）内容实时显示（DeepSeek 的 `delta.reasoning_content`）
- spinner + 分阶段状态：等待网络 → 思考 → 生成回复 → 调用工具
- 本地模型无 reasoning 时优雅降级为普通 spinner
- **token 用量显示**（statusline）：每轮/累计 token 消耗可见，补上"成本可见性"（Loop Engineering 条目，参 Codex/Claude Code 的 statusline）

**关键技术点**：流式下工具调用参数**跨 chunk 碎片化到达**，需按 index 累积拼接（最大坑点）。token 用量从流式响应的 usage 字段获取（部分后端需开启 `stream_options={"include_usage": True}`）。

**动内核**：`LLMClient.complete` 增加流式路径；`AgentLoop.run` yield 更细粒度的增量事件；`Console` 改流式渲染。破铁律第 4 条一次，需保证现有测试仍绿。

**已定决策**：reasoning 思考内容通过配置开关 `show_reasoning` 控制，**默认折叠**（只显示"思考中…" spinner），打开时灰字实时滚动。本地模型无 reasoning 时天然只显示 spinner，无需额外降级逻辑。

**验收标准**：
1. 同一任务发问后 **3 秒内有可见反应**（不再黑屏）
2. 思考过程实时滚动显示（有 reasoning 的模型）
3. 流式下工具调用**正确执行**（碎片拼接无误）
4. 云端 DeepSeek 与本地 LM Studio 都不卡黑屏
5. **token 用量可见**：任务结束后能看到本次消耗的 token 数
6. 现有 28 测试全绿 + 新增流式相关测试

---

## M2.5 — 任务中确认机制升级 + 中断（P1）

**解决**：当前只有 shell 危险命令的 y/n 确认，且仅限 shell。学习 Claude Code 的"人在环中"
授权机制——任务执行到需要授权时弹出多选，用户选择后继续。

### 两层用户选择设计（核心，已实现前半）
把 agent 需要用户介入的场景明确分成两类，语义不混：

| | 层1：模型层澄清 | 层2：工具层权限确认 |
|---|---|---|
| 语义 | 确认**意图**（要什么/怎么做） | 确认**授权**（准不准执行这个危险操作） |
| 触发 | 模型自己判断（提示词驱动） | 工具声明需确认，框架强制拦截 |
| 场景 | 需求歧义、方案选择、业务判断 | 删除/覆盖/移动/执行命令/联网/装依赖 |
| 形式 | 自然语言提问，可列 1/2/3 | 运行时审批（允许/永久允许/拒绝） |
| 代码 | 提示词 `build_system_prompt(interactive)` | `ToolContext.request_confirm(category, msg)` |

- **顺序**：一个操作若既歧义又危险 → 先层1澄清（问清方案），方案定了再调工具（层2拦截确认）。
- **模式差异**：层1澄清在 `chat`（多轮）可提问等待；在 `run`（单次）无法等回答，
  改为**按最合理假设执行并说明假设**（`build_system_prompt(interactive=False)`）。

### ✅ 已实现
- `ToolContext.request_confirm`（允许/永久允许/拒绝三选）+ 按类别记忆"永久允许"。
- ShellTool 改用之；`Console.confirm` 多选交互（提示前停 spinner、补换行防输入污染）。
- 提示词编码两层：层1澄清（chat问/run假设）、层2权限（不自然语言问危险、直接调工具）。
- run/chat 模式经 `_setup→AgentLoop→Conversation→build_system_prompt` 传递。

### ⏸ 待做（M2.5 后半）
- 拒绝时附带说明反馈给模型（当前只是拒绝，未回传原因）。
- 确认机制在更多工具上生效（等 M4 有网络/装依赖等工具时接入）。

**验收标准**：
1. ✅ 危险 shell 命令弹出多选（允许/永久允许/拒绝），选择后正确继续或中止
2. ✅ "永久允许"后同类操作本会话不再询问
3. ✅ 两层语义分开：run 模式遇歧义自行假设、不冗余提问；危险操作走工具层确认
4. ⏸ 拒绝并附说明时，模型能据此换做法
5. ✅ 长任务执行中可用 Ctrl+C 中断，保留已输出、干净收尾（选 Ctrl+C 而非 esc：跨平台可靠）
6. ✅ 现有测试全绿 + 新增确认/两层/模式/中断测试（42 通过）

---

## M3 — 记忆与会话持久化（P1）— 已完成 ✅

**解决**：跨会话零记忆，下次重启忘光。

**已实现**：
- **上下文工程（短期）**：`context.py` 截断从"消息数"升级为 **token 感知**（字符估算，CJK 安全偏保守；保留 system+最近消息应对 lost-in-the-middle）；`max_context_tokens` 配置（默认 8000）。
- **Agent Memory（中期）**：`session/store.py`（JSON，项目 `./.assistant_agent/sessions/`）save/load/list/delete；`Conversation.export_history/load_history` 序列化（不含 system）；CLI：`chat` 默认新会话+每轮自动保存、`chat --resume <id>` 续接、`sessions` 列出、`sessions --delete <id>`（带确认）。
- **长期（向量检索）**：❌ 明确不做。

**验收标准**：
1. ✅ `chat` 退出重启后，`sessions` 能列出历史会话
2. ✅ `--resume <id>` 能恢复并续接（export/load history 实测往返一致）
3. ✅ `sessions --delete <id>` 能删除（带确认）
4. ✅ 上下文截断改为 token 感知（含消息数硬上限兜底、不破坏 tool 配对）
5. ✅ 新增测试全绿（68 通过：test_session/test_context/test_client 等），ruff + 架构测试通过

**顺带还债**：D1（流式碎片拼接补直接单测）、D4（Console.input 收口）已还，见 docs/TECH_DEBT.md。

---

## M4 — 工具集扩展（P1–P2）— 已完成 ✅

**解决**：工具集太窄，做不了真实开发任务。

**已实现**（纯走 `tools/` 扩展点，内核未动；方案见 docs/m4-tools-plan.md）：
- **code_search**（`tools/search.py`）：纯 Python grep，跨平台（Windows 可用），只读不确认；支持 pattern/path/glob/ignore_case/max_results。
- **git 只读**（`tools/git.py`）：单工具 + 子命令白名单（status/diff/log/show/branch）；写操作拒绝；shell=False 防注入；args 经 shlex 解析。

**明确未做**：web_fetch/网络搜索（安全边界不成熟，暂缓）、glob/find_files（可选，暂缓）、git 写操作（不可逆，不做）。

**验收标准**：
1. ✅ 每个新工具各带单元测试（test_search 9 + test_git 7）
2. ✅ 能完成真实链路："搜索代码 → 读取 → 修改 → git diff 确认"（实测通过）
3. ✅ 危险操作纳入机制：git 写子命令被白名单拒绝；只读工具不确认（对齐 Claude Code）
4. ✅ 现有测试仍绿（84 通过），ruff + 架构测试通过

---

## M4.5 — 模型管理与切换（P1）— 已完成 ✅

**解决**：模型切换只能"改 config.yaml + 重启"，不灵活。

**已实现**（方案见 docs/m4_5-model-management-plan.md）：
- **`--provider/-p` 启动标志**：run/chat 临时指定后端，覆盖 config.active，不改文件；非法名报错列可选。
- **对话内 `/model`**：chat 输入 `/model` 弹方向键菜单（复用 questionary）选择、`/model <名>` 直切；**切换保留对话历史**（M3 的 token 截断兜底更小窗口）。
- **`providers` 命令**：列出所有 provider（名/模型/云端或本地/当前标记）。
- **内核轻碰（已批准）**：`AgentLoop.set_client` 仅换 client、不改 run() 控制流，历史天然保留。

**验收标准**：
1. ✅ `--provider <名>` 覆盖生效；非法名报错列可选（实测）
2. ✅ `/model` 菜单/直切，切换保留历史（set_client 单测证明 export_history 前缀不变）
3. ✅ `providers` 列出所有 provider（实测表格含云端/本地/当前）
4. ✅ 切换无副作用、不触发确认
5. ✅ 90 测试全绿，ruff + 架构测试通过；内核仅加 setter，控制流未改

---

## M4.7 — 循环工程与写入安全（P1）— 已完成 ✅

**解决**：真实使用发现 ① agent 复杂循环里自作主张改文件（write_file 无范围限制）；② 用尽轮数硬失败、卡死空耗。

**已实现**（方案见 docs/m4_7-loop-engineering-plan.md）：
- **工作区范围写入**：写项目目录内直接放行，写目录外需确认（对齐 Codex workspace-write / Claude acceptEdits）；靠"流式可见 + Ctrl+C + git"兜底，不逐个弹窗。
- **重复动作熔断**（内核）：连续 3 轮完全相同的工具调用 → 判定卡死终止，不空耗到上限。
- **用尽轮数优雅**（内核）：注入 continue_check——chat 问"继续吗"、run 带如何继续的提示停。
- **`--max-iterations`**：覆盖 config。
- 提示词：只改任务相关文件、改前说明范围。
- 内核改动：run() 加"重复检测 + 用尽续跑"分支 + continue_check 参数，控制流主体未变。

**未做（按方案）**：权限模式（readonly/strict）——可选，后置；OS 沙箱——过重，记为未来信号（不可信/全自动时才需）。

**验收标准**：
1. ✅ 区内写放行、区外写确认、拒绝不写（实测）
2. ✅ 连续相同动作达阈值熔断（单测 client.calls==3）
3. ✅ chat 用尽问续、run 优雅提示停（单测）
4. ✅ --max-iterations 生效（实测）
5. ✅ 96 测试全绿，ruff + 架构测试通过（护栏逼出 console.py 拆分至 formatting.py）

---

## M4.8 — 基础工具补全：局部编辑（P1）— 已完成 ✅

**解决**：只有 write_file 整篇重写——改一行也要模型重输出整个文件，费 token、慢、易误伤其余内容。

**已实现**（方案见 docs/base-tools-plan.md）：
- **edit_file**：精确替换（old_string→new_string），唯一匹配才改（防误替），支持 replace_all；对齐 Claude Edit。
- **multi_edit**：同文件多处替换，顺序应用、原子写入（任一失败整体不改）；对齐 Claude MultiEdit。
- 沿用工作区范围（区外编辑需确认）；提示词引导"局部改优先 edit_file"。
- 调研结论：各家"局部改"本质是 SEARCH/REPLACE（Claude Edit、Roo/Cline apply_diff 底层），
  **不做行号 unified diff**——对本地小模型太脆弱，违背"对笨模型健壮"原则。

**验收标准**：
1. ✅ edit_file 唯一替换；未找到/多次歧义/文件不存在 → 清晰 error（实测）
2. ✅ replace_all 替换所有；multi_edit 原子中止（单测覆盖）
3. ✅ 区外编辑走确认
4. ✅ 110 测试全绿（+8 edit 测试），ruff + 架构测试通过；内核未动

---

## M4.9 — Slash 命令系统（P1）— 已完成 ✅

**解决**：`/model` 等能力用户不知道存在（无可发现性）；控制命令散在 chat 循环的 if 里。

**已实现**（方案见 docs/slash-commands-plan.md）：
- 新增 `cli/commands.py`：SlashCommand/SlashRegistry/ChatContext（仿 ToolRegistry）。
- 内置命令：`/help`（列出全部+说明，可发现性核心）、`/model`、`/sessions`、`/clear`（新会话）、
  `/context`（会话状态/用量）、`/exit`。收编原散落的 /model 与 exit。
- 本地拦截、不进 ReAct、不花 token（对齐 Claude）；未知命令友好提示。
- 基础档（纯打印、鲁棒）；实时下拉菜单（prompt_toolkit）作为后续可选增强，本期不做。

**验收标准**：
1. ✅ `/` 或 `/help` 列出所有命令+说明（实测）
2. ✅ /model /sessions /clear /context /exit 各生效（实测 + 单测）
3. ✅ 未知 /xxx 友好提示、不进 ReAct
4. ✅ slash 本地处理、不调模型（0 token）
5. ✅ 119 测试全绿（+9 命令测试），ruff + 架构测试通过（新增 cli 层）；内核未动

---

## M5 — 上手体验（P2）— 已完成 ✅

**解决**：新用户/新机器上手门槛（安装 + 手动 cp config、填 key）。

**已实现**（方案见 docs/m5-init-plan.md、docs/INSTALL.md）：
- **安装与平台支持**：`docs/INSTALL.md` 各平台步骤 + 矩阵（Windows 原生/Git Bash/WSL2 已实测；Linux/macOS 高置信；Termux 非目标）。
- **`assistant-agent init` 交互向导**（`cli/init.py`）：选后端（云端 OpenAI 兼容/Anthropic/本地）→ 配 model/env/端点 → 检测 → 生成 config.yaml → 校验。
- **安全**：复用 `${VAR}` 展开（不改 schema）；config 只写 `${环境变量名}`，**init 默认不读/不写真实 key**，只检测变量是否已设并给设置指引；本地端点写占位 key；已存在 config 先备份不静默覆盖；端点检测带 timeout + 本地关代理。
- **非交互模式**：暂缓（接口预留）；init 在无 tty 时明确拒绝。

**验收标准**：
1. ✅ init 一路问答生成可用 config.yaml（云端 `${VAR}`／本地占位+api_base；已存在则备份）
2. ✅ 本地端点连通检测（成功报模型数/失败/超时三态，mock 测试覆盖）
3. ✅ 生成配置经 loader 校验通过；生成物不含明文 key（脱敏回归测试）
4. ✅ 135 测试全绿（+16 init 测试），ruff + 架构测试通过；内核未动

---

## 延后（P3，不进近期路线）

- **子 Agent / 多 Agent 编排**：复杂，需求未明确，依赖前面能力成熟。
- **完整沙箱隔离**：仅在"跑不可信任务"时刚需，自用可暂缓（当前靠危险命令确认兜底）。
- **Web GUI**：已决定放到很后面；需先给 Agent 加 HTTP/流式 API 层。

---

## 通用工作纪律（每个里程碑都遵守）

- 改完跑 `pytest` + `ruff`，全绿再算完成（铁律 3、5）。
- 能不动内核就不动；必须动时（如 M2）保证现有测试不回退。
- 每完成一个里程碑做一次 git 提交，留可回退锚点。
