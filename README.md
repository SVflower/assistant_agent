# Assistant Agent

一个跑在本地、**模型后端可自由切换**（云端 OpenAI 兼容 / Anthropic，本地 LM Studio / Ollama / vLLM）的通用任务 Agent，编码能力优先。

核心卖点：换模型只改 `config.yaml`，业务代码零改动。

## 安装

```bash
python -m venv .venv          # Python 3.11+
# 激活 venv 后：
pip install -e ".[dev]"
```

各平台安装步骤、平台支持矩阵与已知坑见 [docs/INSTALL.md](docs/INSTALL.md)（Windows / WSL2 已验证）。

## 配置

**推荐：交互式向导**（选后端、配 key/端点、检测、生成 config.yaml）：

```bash
assistant-agent init
```

或手动复制模板填写：

```bash
cp config.example.yaml config.yaml
```

`config.yaml` 已被 gitignore，不会提交。API key 建议用环境变量（在 YAML 里写 `${ANTHROPIC_API_KEY}`），或直接填入。

切换后端：改 `config.yaml` 顶部的 `active` 字段指向某个 provider 即可。

## 使用

```bash
# 单次任务
assistant-agent run "读取 README.md，在末尾追加一行 changelog，然后列出当前目录确认"

# 交互模式（默认新建会话，自动保存）
assistant-agent chat

# 恢复历史会话续接
assistant-agent chat --resume <会话id>

# 列出 / 删除历史会话
assistant-agent sessions
assistant-agent sessions --delete <会话id>

# 列出 / 恢复中断的任务运行
assistant-agent runs
assistant-agent resume <run-id>

# 模型/后端管理
assistant-agent providers                 # 列出所有 provider
assistant-agent run "..." --provider local_lmstudio   # 临时指定后端（-p，覆盖 active）
# 对话中输入 / 或 /help 查看所有命令（/model 切模型、/clear 新会话、/context 看用量、/sessions、/exit）

# 轮数上限（复杂任务不够时提高）
assistant-agent run "..." --max-iterations 30

# 指定配置文件
assistant-agent run "..." --config /path/to/config.yaml
```

会话存于项目下 `./.assistant_agent/sessions/`（已 gitignore）。

每个用户任务另有独立 Run checkpoint，默认存于 `./.assistant_agent/runs/`。模型完整响应、
授权提示、工具副作用开始和结果确认等边界会原子保存；current 损坏时回退 prev。恢复不会重放
已确认完成的工具。若进程在写文件、Shell、MCP 等副作用开始后退出，结果会标为不确定：交互恢复
必须选择 retry/skip/abort，非交互模式保持暂停。checkpoint 含对话与工具参数，是本地敏感数据，
不等于外部副作用的 exactly-once 事务日志。可在 `agent.recovery` 下关闭或调整目录/保留上限。

运行时边界由配置控制：`agent.max_tool_calls` 限制单任务工具调用总数，
`agent.max_total_tool_output_chars` 限制累计工具结果，`tools.max_output_chars`
限制单次工具结果。Shell/Git 在来源端分别限制 stdout/stderr 捕获；超大输出以有上限的
workspace Artifact 返回，文件数由 `tools.max_artifact_files` 限制。预算耗尽时 Agent 会补齐
当前工具批次的结果并安全终止。

`read_file` 支持 1-based `start_line`/`end_line` 范围读取并返回总行数与下一页提示；
`code_search` 支持 `context_lines`。所有工具参数会在授权和副作用前按 JSON Schema 校验，
错误带稳定 code/retryable；write/edit/multi_edit 使用同目录临时文件和原子替换。

## 技能（Skills）

把某类任务的做法手册写成 `SKILL.md`，放到 `./.assistant_agent/skills/<名>/`
（项目级）或 `~/.assistant_agent/skills/<名>/`（个人级），Agent 会自动发现。
个人 Skill 默认受信；项目/自定义 Skill 会在元数据进入模型上下文前聚合确认，也可通过
`skills.trusted_project_skills` 按名称显式信任。渐进披露：启动只注入已授权技能的
name/description（省 token），模型判断相关时才
`load_skill` 加载正文照做；正文可指向脚本/参考文件，用现有工具读或跑。
对话中输入 `/skills` 查看已发现的技能。格式与 Claude Code 的 SKILL.md 兼容。

```markdown
---
name: run-tests
description: 如何在本项目跑测试与 lint。当用户要验证改动或跑测试时使用。
---
# 跑测试
1. `pytest -q`
2. `ruff check src tests`
```

## MCP（外部工具接入）

通过 [MCP](https://modelcontextprotocol.io) 协议接入外部工具生态（浏览器自动化、
数据库、第三方 API）。在 config 的 `mcp.servers` 下配置本地 stdio server：

```yaml
mcp:
  enabled: true
  servers:
    playwright:
      command: npx
      args: ["-y", "@playwright/mcp@latest"]
      auto_approve: false   # true 表示信任该 server 的全部当前工具（高风险）
```

其工具以 `mcp__<server>__<tool>` 注册，与内置工具同流（审计/预算/确认）。
每个 MCP 工具默认逐次确认，参数会递归脱敏并限长；"永久允许"按 server、tool 和精确参数
记忆，不会扩散到其他调用。未信任 server 的 annotations 不能降低权限等级。
用 `include_tools`/`exclude_tools`、`max_tools`、`max_total_tools` 控制接入的工具集。
远程 server 用 `type: http` + `url`（Streamable HTTP），headers 支持 `${VAR}` 注入 token：

```yaml
mcp:
  servers:
    remote_api:
      type: http
      url: "https://mcp.example.com/mcp"
      headers:
        Authorization: "Bearer ${MCP_TOKEN}"   # 真值从环境取，不落配置
```

对话中输入 `/mcp` 查看已接入的 server 与工具。session/协议头/重连由 SDK 代管，调用不自动重放。

## 权限边界

所有内置和 MCP 工具都在 Registry 执行前经过同一套 `deny -> ask -> allow` 权限策略。
默认 `permissions.mode: workspace`：工作区内置文件读写允许，区外访问、任意 Shell、网络和
未信任 MCP/项目 Skill 需要确认；非交互模式无法确认时会拒绝。另有 `readonly`、`strict`、
`unrestricted` 模式及 capability 规则。该机制是可审计的应用层权限门，不是 OS 沙箱；
获准进程仍拥有启动 Agent 的系统用户权限。

## 开发

```bash
pytest                          # 跑测试
pytest --cov                    # 覆盖率
ruff format . && ruff check .   # 格式化 + lint
python -m mypy src/assistant_agent
```

### Agent 行为评测

```bash
python -m evals scripted
python -m evals recovery
python -m evals real --config config.yaml --provider local --repeat 3
python -m evals compare evals/reports/run-a/results.jsonl evals/reports/run-b/results.jsonl
```

`scripted` 使用确定性脚本验证真实 AgentLoop 的工具轨迹、权限、预算、终止协议和文件副作用；
`recovery` 在真实 checkpoint 边界注入崩溃，验证不重放和预算恢复。
确定性评测已接入 CI，但不代表模型能力。`real` 才调用配置中的真实 provider，结果可能波动，不作为 PR
硬门；外部 Skills/MCP 默认关闭，需要时显式加 `--skills` / `--mcp`。当前没有 OS 沙箱，真实
评测只应运行仓库自有、可信的小型 fixture。

## 架构

```
config/   配置加载与校验（Pydantic + YAML）
cli/      CLI 层：slash 命令系统（/help /model 等）+ init 配置向导 + Runtime 生命周期
llm/      模型抽象层（封装 LiteLLM，统一云端/本地）
tools/    工具系统（base/registry + 内置：读/写/局部编辑/多处编辑/列目录/shell/代码检索/git 只读/澄清）
session/  Session 存档 + Run checkpoint 双槽原子存储
skills/   Agent Skills（SKILL.md 发现 + 渐进披露 + load_skill）
mcp/      MCP client（stdio + HTTP transport 接外部工具生态，同步桥 + 命名空间）
obs/      结构化 JSONL 事件日志与工具审计（尽力脱敏，禁用零副作用）
agent/    ReAct 主循环 + RunState/恢复协调 + 上下文管理（token 截断/摘要）+ 提示词
ui/       终端输入输出（Rich 流式渲染）
```

扩展点：换模型动 `config.yaml`；加能力优先在 `tools/` 加文件并在 `registry.py` 注册，或接 `skills/`（SKILL.md）与 `mcp/`（外部 server）——内核 `agent/loop.py` 通常不必动（确需演进时先确认）。

第三阶段“可信执行与质量闭环”已完成：M9a-M10b 已交付，M10c 决定暂不进行全栈 async 重构，
D18 进程树终止边界保留待独立立项。当前 392 个测试通过（2 个平台能力测试跳过）、覆盖率 78%、
6744 行生产 Python 源码 + 1366 行 eval 基础设施。详见
[第三阶段规划](docs/phase3-trustworthy-agent-plan.md)。

详见 [DESIGN.md](DESIGN.md)。
