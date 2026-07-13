# Assistant Agent — 设计文档

> 一个跑在本地、模型后端可自由切换（云端 API key / 本地推理服务器）的通用任务 Agent。
> 编码/开发能力优先。

状态：第一阶段（MVP→M5）全部完成；第二阶段进行中（M6 结构化日志已落地）。**最新里程碑进展以 ROADMAP.md 为准**——本文档正文第 8/9 节是第一阶段的设计快照，保留作历史记录，不随里程碑更新。
最后更新：2026-07-11

---

## 1. 要解决的问题

一个跑在你自己机器上、**模型后端可自由切换**的通用任务 Agent。它解决三个痛点：

- **不被单一厂商锁定**：今天用 Claude API，明天切本地模型省钱，只改配置，业务逻辑不动。
- **隐私 / 离线可用**：敏感任务可全程走本地模型，数据不出机器。
- **能真正动手干活**：不只是聊天，能读写文件、执行命令、自主完成多步任务。

**定位**：通用任务 Agent，编码/开发能力优先。

---

## 2. 核心能力（分层）

### 地基层（MVP 必须有）
- **模型抽象层**：统一接口，背后可以是 API key（OpenAI/Anthropic/…）或本地（LM Studio/vLLM）。
- **Agent 循环（ReAct）**：观察 → 推理 → 调工具 → 再观察，直到任务完成。
- **工具系统**：可注册、可扩展。最小四件套：读文件、写文件、列目录、执行命令。
- **配置系统**：一个配置文件管 provider、模型名、API key、本地 endpoint、参数。
- **上下文 / 对话管理**：累积历史，超长时截断或摘要。

### 产品层（后续迭代，MVP 不做）
- 持久化记忆（跨会话）
- 子 Agent / 任务编排
- 工具权限与沙箱
- 网络搜索、HTTP 请求工具
- 流式输出、TUI 界面

---

## 3. 技术栈

| 选择 | 方案 | 理由 |
|------|------|------|
| 语言 | **Python 3.11+** | LLM 生态最成熟；本地模型工具 Python 优先；工具调用 / JSON schema 处理方便。 |
| 模型统一层 | **LiteLLM** | 一个库统一 100+ 厂商 API **和**本地模型，全走 OpenAI 兼容格式。"配 key 或配本地"靠它一行切换。 |
| 本地后端 | **LM Studio**（默认 `localhost:1234/v1`）/ **vLLM** | 均暴露 OpenAI 兼容端点，架构与云端完全一致，仅 `base_url` 不同。 |
| 配置 | **Pydantic + YAML** | Pydantic 校验 + 类型安全；YAML 给人读、改 provider 方便。 |
| CLI | **Typer**（起步）→ Textual（后续 TUI） | 起步快，后续可升级界面。 |
| 工具调用 | **原生 function calling** | 比解析文本可靠得多。 |

### 核心洞察
几乎所有本地推理服务器（LM Studio、vLLM、Ollama、llama.cpp）都暴露 **OpenAI 兼容的 `/v1/chat/completions` 端点**。本地模型和云端 API 在代码里**长得一模一样**，区别只是 `base_url` 和 `api_key`。LiteLLM 把这层差异也吃掉。
**原则：绝不在业务逻辑里写死任何 provider。**

---

## 4. 目录结构

```
assistant_agent/
├── pyproject.toml              # 依赖与项目元数据
├── config.example.yaml         # 配置模板（不含密钥，提交进仓库）
├── config.yaml                 # 实际配置（含密钥，加进 .gitignore）
├── .env.example                # API key 也可走环境变量
├── README.md
├── DESIGN.md                   # 本文档
│
├── src/
│   └── assistant_agent/
│       ├── __init__.py
│       ├── main.py             # CLI 入口
│       │
│       ├── config/
│       │   ├── schema.py       # Pydantic 配置模型
│       │   └── loader.py       # 加载 + 校验 config.yaml / env
│       │
│       ├── llm/
│       │   └── client.py       # 模型抽象层（封装 LiteLLM，统一 API/本地）
│       │
│       ├── agent/
│       │   ├── loop.py         # ReAct 主循环
│       │   ├── context.py      # 对话历史 / 上下文管理
│       │   └── prompts.py      # 系统提示词
│       │
│       ├── tools/
│       │   ├── base.py         # 工具基类 + 注册表
│       │   ├── registry.py     # 工具注册与 schema 生成
│       │   ├── file_ops.py     # 读 / 写 / 列目录
│       │   └── shell.py        # 执行命令（带确认机制）
│       │
│       └── ui/
│           └── console.py      # 终端输入输出
│
└── tests/
    ├── test_tools.py
    ├── test_config.py
    └── test_loop.py
```

### 设计要点
`llm/` 和 `tools/` 是两个隔离的扩展点。
- 换模型 → 只动 `config.yaml`
- 加能力 → 只在 `tools/` 加文件并注册
- 内核 `agent/loop.py` 职责稳定、通常不必动；确需演进（预算/终止/恢复）时受控进行，改前先确认

---

## 5. MVP 范围

**目标**：一个能在终端跑、可在云端 API 和本地后端之间一键切换、能用文件和命令工具完成多步任务的 Agent。

### 做（In scope）
- [x] 配置系统：YAML 配 provider，支持 API key 和本地 endpoint 两种模式
- [x] 模型抽象层：通过 LiteLLM 统一调用，验证"切换后端业务代码不变"
- [x] ReAct 主循环：支持多轮工具调用直到任务完成
- [x] 四个核心工具：读文件、写文件、列目录、执行 shell 命令
- [x] shell 工具的危险操作确认（删除/覆盖/移动等先问用户，其余直接执行）
- [x] 简单 CLI 交互：输入任务 → 看 Agent 一步步执行 → 输出结果
- [x] 基础测试：工具、配置、循环各一组

### 不做（Out of scope，留给迭代）
- [ ] 跨会话持久化记忆
- [ ] 子 Agent / 多 Agent 编排
- [ ] 完整沙箱隔离（MVP 只做"确认提示"级别）
- [ ] 网络搜索、浏览器、复杂工具
- [ ] 图形界面 / Web 界面
- [ ] 流式 token 输出（先用整段返回，简单可靠）

### 验收标准
同一个任务（例如"读 README，在末尾加一行 changelog，然后列出目录确认"），分别用**云端 API** 和**本地后端**各跑一遍，都能正确完成——**且只改了 `config.yaml`，没动任何代码。**

---

## 6. 已确认的决策

| 项 | 决策 |
|----|------|
| 用途 | 通用任务 Agent，编码/开发优先 |
| 语言 | Python 3.11+ |
| 可测后端 | 云端 API（有 key）+ 本地 LM Studio / vLLM |
| shell 安全级别 | 危险命令才确认（删除/覆盖/移动），其余直接执行 |

---

## 7. 风险

### 技术风险
- **本地小模型工具调用不稳（最大风险）**：本地 7B/8B 模型常不会乖乖输出工具调用格式。
  - 缓解：选明确支持 function calling 的 instruct 模型（Qwen2.5-Instruct、Llama-3.x-Instruct 等）；ReAct 循环做容错解析与重试；工具 schema 写清晰。
  - **MVP 阶段最该先验证此点。**
- **上下文窗口差异大**：本地模型上下文常远小于云端。
  - 缓解：上下文管理层做长度感知截断。
- **安全**：执行 shell + 文件写入本身就是高危能力。
  - MVP 用"确认机制"兜底，但**这不是真沙箱**。若要跑不可信任务，后续必须上隔离。

---

## 8. 第一阶段（MVP）— 已完成 ✅

编码顺序（每步已独立验证）：
1. ✅ 配置系统 + 模型抽象层 → "能调用到模型"（云端 DeepSeek + 本地 LM Studio 各验证）
2. ✅ 工具系统 + 四个核心工具 → 单元测试覆盖
3. ✅ ReAct 主循环 → 串起来，完成验收任务
4. ✅ CLI + README + 终端 banner（Agent/model/位置）

**验收结果**：同一多步任务在云端 DeepSeek 和本地 LM Studio 各跑通，只改 `config.yaml` 的 `active`，业务代码零改动。核心卖点成立。

**阶段内修复的真实问题**：
- Windows 系统代理导致本地请求 502 → `llm/client.py` 自动把本地 host 加入 `NO_PROXY`
- GBK 终端崩溃 → stdout 重配 UTF-8
- shell 交互命令卡死超时 → `stdin=subprocess.DEVNULL`
- 模型用错命令语法 / 答不出日期 → 系统提示词注入 OS 与当前日期
- shell 输出中文乱码 → 平台感知编码容错 `_decode`

状态：28 个测试通过，ruff 全绿。

---

## 9. 第二阶段 — 流式输出（进行中规划）

**目标**：让"等待"与"思考"过程透明，解决当前"发问后长时间黑屏"的体验问题。

**调研结论**（Codex / Claude Code 经验，见 docs 相关笔记）：靠三层叠加——
1. 流式 token 输出（`litellm` `stream=True`，已实测 DeepSeek 首 chunk ~2.4s、总 ~6.2s，流式可显著改善等待感）
2. 动画 spinner + 状态提示（覆盖网络往返、首 token 前的空窗）
3. 分阶段状态：等待网络 → 思考(reasoning)→ 生成回复 → 调用工具

**技术要点**：
- DeepSeek 的 `delta.reasoning_content` 可流式获取（已验证）；本地模型可能无 reasoning，需优雅降级
- 流式下工具调用参数是**跨 chunk 碎片化到达**的，需按 index 累积拼接（最大坑点）
- 会改动 `agent/loop.py` 内核（破铁律第 4 条一次），需保证现有测试仍绿并补流式测试

**待定决策**：reasoning 思考内容默认展示还是折叠。

