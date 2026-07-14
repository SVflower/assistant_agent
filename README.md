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

运行时边界由配置控制：`agent.max_tool_calls` 限制单任务工具调用总数，
`agent.max_total_tool_output_chars` 限制累计工具结果，`tools.max_output_chars`
限制单次工具结果。预算耗尽时 Agent 会补齐当前工具批次的结果并安全终止。

## 技能（Skills）

把某类任务的做法手册写成 `SKILL.md`，放到 `./.assistant_agent/skills/<名>/`
（项目级）或 `~/.assistant_agent/skills/<名>/`（个人级），Agent 会自动发现。
渐进披露：启动只注入技能的 name/description（省 token），模型判断相关时才
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
      auto_approve: false   # 每次调用都需确认（安全默认）
```

其工具以 `mcp__<server>__<tool>` 注册，与内置工具同流（审计/预算/确认）。
每个 MCP 工具默认逐次确认，"永久允许"按 server+tool 粒度记忆，不会一次放行全部。
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

## 开发

```bash
pytest                          # 跑测试
ruff format . && ruff check .   # 格式化 + lint
```

## 架构

```
config/   配置加载与校验（Pydantic + YAML）
cli/      CLI 层：slash 命令系统（/help /model 等）+ init 配置向导 + Runtime 生命周期
llm/      模型抽象层（封装 LiteLLM，统一云端/本地）
tools/    工具系统（base/registry + 内置：读/写/局部编辑/多处编辑/列目录/shell/代码检索/git 只读/澄清）
session/  会话持久化（JSON 存档，跨会话续接 + 摘要 checkpoint）
skills/   Agent Skills（SKILL.md 发现 + 渐进披露 + load_skill）
mcp/      MCP client（stdio + HTTP transport 接外部工具生态，同步桥 + 命名空间）
obs/      结构化 JSONL 事件日志与工具审计（尽力脱敏，禁用零副作用）
agent/    ReAct 主循环 + 上下文管理（token 感知截断 + 摘要压缩）+ 提示词
ui/       终端输入输出（Rich 流式渲染）
```

扩展点：换模型动 `config.yaml`；加能力优先在 `tools/` 加文件并在 `registry.py` 注册，或接 `skills/`（SKILL.md）与 `mcp/`（外部 server）——内核 `agent/loop.py` 通常不必动（确需演进时先确认）。

第三阶段“可信执行与质量闭环”已完成规划但尚未实施：先修上下文/会话/生命周期硬正确性，
再建设统一权限、行为级 eval、大文件工具契约与可恢复执行。详见
[第三阶段规划](docs/phase3-trustworthy-agent-plan.md)。

详见 [DESIGN.md](DESIGN.md)。
