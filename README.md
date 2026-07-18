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

# 脚本/管道场景：隐藏过程轨迹，只输出最终回答（失败诊断仍保留）
assistant-agent run "总结 README.md" --quiet

# 交互模式（默认新建会话，自动保存）
assistant-agent chat

# Windows 开发环境也可直接使用项目虚拟环境启动同一入口
.venv\Scripts\python -m assistant_agent chat

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
# 使用 /display normal|verbose|quiet 即时切换当前会话的展示详细度

# 轮数上限（复杂任务不够时提高）
assistant-agent run "..." --max-iterations 30

# 指定配置文件
assistant-agent run "..." --config /path/to/config.yaml
```

CLI 默认使用 `normal`：模型生成过程在临时活动区流式显示，工具调用后不留冗余旁白，终端历史只保留
语义工具轨迹和最终 Markdown 回答；`verbose` 额外保留完整过程及有界、脱敏的工具详情；`quiet`
隐藏过程轨迹，适合脚本消费。可在对话中用
`/display normal|verbose|quiet` 临时切换，也可在配置中设置默认值：

```yaml
ui:
  display_mode: normal  # normal | verbose | quiet
```

会话、Run、日志和工具产物按工作区隔离存于
`~/.assistant_agent/workspaces/<workspace-id>/`；可用 `ASSISTANT_AGENT_HOME` 覆盖用户状态根目录。

每个用户任务另有独立 Run checkpoint。模型完整响应、
授权提示、工具副作用开始和结果确认等边界会原子保存；current 损坏时回退 prev。恢复不会重放
已确认完成的工具。若进程在写文件、Shell、MCP 等副作用开始后退出，结果会标为不确定：交互恢复
必须选择 retry/skip/abort，非交互模式保持暂停。checkpoint 含对话与工具参数，是本地敏感数据，
不等于外部副作用的 exactly-once 事务日志。可在 `agent.recovery` 下关闭或调整目录/保留上限。

运行时边界由配置控制：`agent.max_tool_calls` 限制单任务工具调用总数，
`agent.max_total_tool_output_chars` 限制累计工具结果，`tools.max_output_chars`
限制单次工具结果。Shell/Git 在来源端分别限制 stdout/stderr 捕获；超大输出以有上限的
workspace Artifact 返回，文件数由 `tools.max_artifact_files` 限制。预算耗尽时 Agent 会补齐
当前工具批次的结果并安全终止。

内置工具执行环境由 `sandbox.mode` 选择：`off` 保持宿主兼容；`workspace` 强制文件和 cwd 不越过
当前项目，但它不是 OS 沙箱；`container` 使用 Docker/Podman，把 Shell/Git 放入临时容器。容器
默认只挂载当前项目、禁网、非 root、清空 capabilities，并限制 CPU/内存/PID；退出、超时或中断
会强制销毁。Web、外置 MCP server 和自定义 Python Tool 仍在宿主机运行，不继承容器隔离。

```yaml
sandbox:
  mode: container       # off | workspace | container
  engine: docker        # docker | podman
  image: python:3.11-slim
  network: none
  memory: 1g
  cpus: 1.0
  pids_limit: 256
  user: auto            # 禁止 root
```

`read_file` 支持 1-based `start_line`/`end_line` 范围读取并返回总行数与下一页提示；
`code_search` 支持 `context_lines`。所有工具参数会在授权和副作用前按 JSON Schema 校验，
错误带稳定 code/retryable；write/edit/multi_edit 使用同目录临时文件和原子替换。

## 技能（Skills）

把某类任务的做法手册写成 `SKILL.md`，放到 `./.agents/skills/<名>/`
（项目级）或 `~/.assistant_agent/skills/<名>/`（个人级），Agent 会自动发现。旧的
`./.assistant_agent/skills/` 仅做最低优先级只读兼容，不会自动移动或删除。
个人 Skill 默认受信；项目/自定义 Skill 会在元数据进入模型上下文前聚合确认，也可通过
`skills.trusted_project_skills` 按名称显式信任。渐进披露：启动只注入已授权技能的
name/description（省 token），模型判断相关时才
`load_skill` 加载正文照做；正文可指向脚本/参考文件，用现有工具读或跑。
对话中使用 `/skills list|install|remove|doctor` 管理；安装默认进入 user scope，项目安装需显式
指定 `project`。受管安装校验清单、大小和符号链接，卸载不会删除用户手写目录；变更在下次启动生效。
格式与 Claude Code 的 SKILL.md 兼容。

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
  connect_parallelism: 4
  servers:
    playwright:
      command: npx
      args: ["-y", "@playwright/mcp@latest"]
      startup: optional      # optional 失败降级；required 使当前 Runtime 创建失败
      connect_timeout: 5     # 连接/初始化/工具发现，与调用 timeout 分离
      auto_approve: false   # true 表示信任该 server 的全部当前工具（高风险）
```

其工具以 `mcp__<server>__<tool>` 注册，与内置工具同流（审计/预算/确认）。
每个 MCP 工具首次调用可选“允许一次 / 本会话允许此工具 / 本会话信任此 server / 拒绝”；
记忆键按 server/tool 或 server，不再把变化的参数混入授权 scope。参数仍会递归脱敏并进入审计，
显式 deny/ask 规则优先，未信任 server 的 annotations 不能降低权限等级。
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

对话中使用 `/mcp list|add|test|doctor|enable|disable|trust|untrust|remove` 管理 server；
`/mcp add playwright` 会隔离探测后原子写入 user 配置，新增工具在下次启动生效。stdio server 使用
最小环境和受管 cwd，Playwright snapshot/screenshot 默认进入 workspace MCP artifact，不落仓库根。
卸载默认保留历史 artifact；`remove ... --purge-artifacts` 需要二次确认且只清理当前 server。
session/协议头/重连由 SDK 代管，调用不自动重放。
多个 server 在专用 event loop 内有界并行连接，工具仍按配置顺序稳定注册。optional server 离线只产生
结构化降级状态，不阻止普通 Agent；required server 失败只阻止当前 Runtime。长期服务可通过公共
`RuntimePolicy` 限制 transport、个人 Skill、扩展管理和最低 sandbox，并读取脱敏能力快照。

## 联网检索

内置 `web_search` 与 `fetch_url` 用于时效性搜索和来源核验。默认 DuckDuckGo backend 无需 key，
也可在 `web.search.backend` 切换到 SearXNG；工具返回查询/抓取时间和来源 URL。网页抓取仅允许
HTTP(S)，拒绝 localhost/私网、URL 凭据和危险重定向，并限制超时、响应字节和正文长度。

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
硬门；外部 Skills/MCP 默认关闭，需要时显式加 `--skills` / `--mcp`。真实评测仍应使用受信 fixture，
并按风险选择 workspace/container sandbox。

## 架构

```text
contracts/      稳定公共 DTO、事件、错误与 Interaction 协议
agent/          ReAct Loop、Context 与 Run 状态机
application/    Runtime、Session、Run 用例和消费方 ports
bootstrap/      唯一 composition root，装配所有具体资源
service/        面向 API/其他 Python 调用方的稳定 re-export
providers/      模型端口与 LiteLLM adapter
tools/          工具模型、端口、权限、Registry 与内置能力
execution/      Workspace、进程监管、暂停/取消和容器执行
persistence/    Session、Run checkpoint 与 Artifact 存储
observability/  结构化日志、审计与脱敏 adapter
integrations/   MCP、Skills 与 Web Access 外部系统 adapter
cli/ + ui/      终端命令、交互端口与 Rich 渲染
config/         Pydantic/YAML 配置与路径策略
```

架构采用 Ports and Adapters，依赖规则由 import-linter 强制；完整所有权、依赖图和拆分准则见
[架构事实源](docs/ARCHITECTURE.md)。外部服务只应依赖 `assistant_agent.service`、
`assistant_agent.contracts` 和必要的 `assistant_agent.interaction` 安全实现。

业务 MCP 与 Agent 仓库分开存放，独立安装、测试和运行。它们不进入 `assistant_agent` Python 包，
只通过标准 MCP 接入。Agent 的 MCP client 支持稳定调用 ID 透传、受信 tool annotations、按工具
恢复/超时策略、写调用结果未知保护和结构化输出契约校验。

简单 Python 工具可通过公开的 `@agent_tool` / `FunctionTool` API 从类型注解生成 Schema；注册后仍
完整经过 Registry 的参数校验、权限、预算、审计、observer 和恢复链路。未声明权限时沿用未知扩展
工具的保守权限声明，最终行为由统一权限策略决定。

扩展点：换模型动 `config.yaml`；加本地能力优先在 `tools/` 加文件并注册，外部能力通过
Skills/MCP 接入。`agent/loop.py` 保留为可见内核；预算、终止、恢复等确需演进时先确认并跑行为评测。

第三阶段“可信执行与质量闭环”已完成：M9a-M10b 已交付，M10c 决定不进行全栈 async 重构。
第四阶段 M11a-M11c 已完成 CLI 展示、可信联网检索和
MCP/Skill 自助管理。第五阶段 M12a 已完成 provider-neutral 的 MCP 运行时安全语义；第六阶段
M13a 已完成声明式工具适配层；第七阶段 M14 已完成暂停/取消、进程树监管与可选容器 Workspace，
还清 D18。第八阶段 M15 已完成统一 CLI 活性反馈、动态阶段计时、正文停更后的模型生成提示、
授权后恢复动画和关键外部动作摘要。第九阶段 M16 已完成 UI 无关 Runtime 工厂、同步
InteractionPort、Session/Run 公共服务门面和稳定 StepEvent 契约，可供安装后的 Python 包直接调用。
第十阶段 M17 已完成部署级 RuntimePolicy、MCP 有界降级启动和脱敏能力快照。第十一阶段 M18
已完成结构化失败、安全 activity/预算快照，以及可 checkpoint 恢复的三类预算 continuation。
第十二阶段 M19 已完成 contracts/agent/application/bootstrap/service 与 adapter 包重建，旧路径保持
薄兼容。当前 606 个测试通过（5 个平台能力测试跳过）、覆盖率 84%、13974 行生产 Python 源码 +
1366 行 eval 基础设施；scripted 18/18、recovery 4/4。详见
[M19 架构](docs/ARCHITECTURE.md)和[通用服务调用指南](docs/agent-service-integration-guide.md)。

详见 [DESIGN.md](DESIGN.md)。
