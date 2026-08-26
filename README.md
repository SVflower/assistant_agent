# Assistant Agent

一个跑在本地、**模型后端可自由切换**（云端 OpenAI 兼容 / Anthropic，本地 LM Studio / Ollama / vLLM）的通用任务 Agent，编码能力优先。

核心卖点：换模型只改 `config.yaml`，业务代码零改动。

面向企业工业问答、知识/MCP/分析模块化接入与工业数据分析的长期产品方向，见
[工业智能 Agent 产品蓝图](docs/INDUSTRIAL_AGENT_PRODUCT_BLUEPRINT.md)。该文档描述目标产品和演进路线，
当前已交付能力仍以 [ROADMAP.md](ROADMAP.md) 为准。

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

`run_shell` 是有界前台命令：配置的 timeout 覆盖执行、输出排空和进程树清理；若命令用
`start /b`、`nohup` 或 `&` 遗留继承管道的后台进程，Agent 会终止该进程树并返回结构化错误，
不会无限等待。需要让开发服务器跨多个步骤运行时，Agent 使用 `manage_process` 显式启动、查看状态/
有界日志和停止。后台进程只属于当前 Runtime，使用 opaque process ID，Runtime 关闭时全部清理；
容器 Workspace 暂不提供跨步骤后台进程。

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

把某类任务的做法手册写成 `SKILL.md`，放到 `./skills/<名>/`
（项目级）或 `~/.assistant_agent/skills/<名>/`（个人级），Agent 会自动发现。
个人 Skill 默认受信；通过 `/skills install ... project` 安装的项目 Skill 会在安装确认时记录信任，
手工复制的项目/自定义 Skill 仍需显式信任。Skill 目录变化会在下一轮对话前自动刷新；渐进披露只注入已授权技能的
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

第一次阅读本项目，建议从 [Assistant Agent 学习手册](docs/learning/README.md) 开始。手册按真实调用链
讲解 Python 写法、架构、Agent Loop、工具安全链、Session/Run 恢复和 MCP 等集成，并给出调试练习。

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
InteractionPort、Session/Run 公共服务门面和稳定 ItemEvent 契约，可供安装后的 Python 包直接调用。
第十阶段 M17 已完成部署级 RuntimePolicy、MCP 有界降级启动和脱敏能力快照。第十一阶段 M18
已完成结构化失败、安全 activity/预算快照，以及可 checkpoint 恢复的三类预算 continuation。
第十二阶段 M19 已完成 contracts/agent/application/bootstrap/service 与 adapter 包重建，并删除
全部迁移前内部包和转发文件。开发期只维护目标结构；服务调用方固定使用 `service/contracts/interaction`
公共根入口。第十三阶段 M20 已将 optional MCP 从启动关键路径移除：有目录时注册稳定 Schema 并在
首次调用连接，无目录时后台发现、下一 Runtime 生效；required MCP 仍同步校验，Skill 正文仍按需加载。
模型通过只读 `inspect_runtime` 查询当前工具、Skill、MCP 动态状态，不再搜索项目文件猜测自身能力。
第十四阶段 M21 已修复父进程先退出、后代继承 PIPE 导致的无限等待，并增加 Runtime 隔离的
`manage_process` 后台进程生命周期；前台命令和后台服务不再混用逃逸语法。
第十五阶段 M24 已增加受控 `ChartSpecV1`/不可变 Chart Artifact 与声明式 `present_chart`；图表先进入
RunState v4 原子 checkpoint 再发布事件，终态同步 Session，公共服务提供 list/get/snapshot，API 无需
读取内部文件。第十六阶段 M25 增加服务器 Web Runtime profile：工具注册白名单移除文件、Shell、进程
与扩展管理能力，安全搜索不再弹审批，Interaction 提供截止时间并可被 pause/cancel 唤醒。
M25-AGENT-02 进一步支持 paused Run 跨 Runtime 指定取消，并由 Agent 在事件源异常时权威持久化 failed
终态，API 不再复制终态状态机。第十七阶段 M22 增加跨进程 Session execution lease、checkpoint v6、
遗留 running Run 协调和 failed Run 安全幂等重试。M23-R1 Agent 侧进一步提供 Session schema v1、
自动标题、用户 rename CAS、服务端搜索/keyset catalog 和权威 last_run；所有 Session 写路径使用短时
跨进程 lifecycle 锁、tombstone 和 fresh merge，终态同步不覆盖并发 rename，删除后旧 Runtime 不复活
Session/Run；Run 单删也持久 tombstone，CLI 删除统一走服务用例；旧 naive 时间固定解释为 UTC 且
小数秒不截断；`get_session_summary` 直接按 ID 从线性化锁内快照构造公开 DTO。M23-R2 Agent 侧进一步
把 Session schema/contract 升至 v2，以权威 ledger 持久化稳定 message ID/time/reply，并提供绑定源
Session 的原子幂等 fork；fork 严格排除边界、深复制 Chart Artifact、设置 `run_id=null`，且不复制
Run/Interaction/compaction。Event v1、RunState v6 与 Agent Loop 不变，API/Web 待按交接文档接入。
M28 曾新增逐字节兼容 V1 的受控 ChartSpecV2、15 类普通图表与确定性数据派生；M30 收紧 Heatmap
新建路径为 X/Y category axis，拒绝空数据、全 null value 和空白坐标，并提供按图表意图隔离、可恢复的
一次定向纠错 metadata。Event v1、Session contract/schema v3、RunState v7 保持不变。
M31-M37 开发期 hard cut 后，当前只支持 RunState v13、Session v5、Chart V2、Attachment/Content v1
和 Managed Output v1；旧格式不迁移，版本不匹配 fail closed。HTML/CSV/JSON/Markdown/文本交付物
统一写入可配置的 `<workspace>/outputs/YYYY/MM/DD/<session-id>/`。`create_output` 仅声明元数据，
Runtime 原生捕获下一轮正文；公共契约不暴露服务器路径或草稿协议。
RunState v13 额外保存用户实时看到的有界 `ReasoningPresentationV1`，供刷新后恢复 Thinking；该记录
不进入 Session ledger、后续模型上下文、trajectory、日志或普通导出。
Native ArtifactWriter 会在正式发布前验证 HTML/JSON/CSV/Markdown/纯文本的基础结构；无效草稿不会
进入 Run、Session 或 API/Web，验证事实只以稳定错误码进入安全 trajectory。
M34 新增 Observability v1：RunSnapshot 与非高频 ItemEvent 提供受控 timing、context、model usage 和
有界 trajectory；Provider 未报告的指标保持 null。简单任务的 TaskPlan 为 null，多步骤交付任务通过
`update_task_plan` 显式维护；当前没有 trajectory 分页接口。
M35-R1 进一步让公开 user/assistant 消息以 nullable `run_id` 权威关联原 Run，并由 RunSnapshot 暴露
安全创建时间和 provider/model；旧消息不猜测、fork 清空关联。该能力受默认全局 100 个 terminal Run
与每 Run 256 条 trajectory 保留限制，不是完整长期 TraceStore。
M36 沿用 ChartSpecV2 现有 axis title、column unit 和 scale：Agent 确定性生成带单位的 X/Y 轴标题，
保持 datetime/category/linear 语义，并强化 SPC 多面板生成规则；Web 负责按真实标签密度和容器尺寸
自适应旋转、抽样与边距，不向模型开放 ECharts grid/formatter/style。
Performance Foundation P0 为 Observability v1 additive 增加 Agent 自身 context/checkpoint/Session sync
分段指标，并将纯 telemetry 改为搭载到既有恢复边界；确定性场景 checkpoint 分别为 4（单轮）、
9（两轮一工具）、14（三轮纠错）。`present_chart` 的 line/area/bar 现在可用 x_key+y_key 安全生成单系列，
多系列、聚合和轴语义仍禁止猜测。公共版本不变，未修改 Agent Loop。
当前 859 个测试通过（10 个平台能力测试跳过）、覆盖率 84%、19,753 行/138 文件生产 Python 源码 + 1,411 行 eval
基础设施；scripted 23/23、
recovery 4/4；剩余 8 项技术债（5 中/3 低，无高优先级）。详见
[架构事实源](docs/ARCHITECTURE.md)和[通用服务调用指南](docs/agent-service-integration-guide.md)。

详见 [DESIGN.md](DESIGN.md)。
