# M11b/M11c 联网检索与 MCP 自助接入方案

> 状态：已完成并验收  
> 日期：2026-07-15  
> 前置：M11a 已完成  
> 内核影响：本方案不修改 `agent/loop.py`

> 验收：467 passed、3 skipped、覆盖率 82%；Ruff/mypy、18/18 scripted eval、4/4 recovery
> eval 全绿。隔离 HOME 中 Playwright MCP 完成安装、24 工具发现、导航/快照、重启发现和卸载；
> Skill user/project 安装、加载、遮蔽回退与卸载通过。未修改 `agent/loop.py`。

## 1. 问题与目标

当前 Agent 能通过 Shell 间接执行网络命令，也已经能连接预先写入 `config.yaml` 的 MCP server，
但尚不具备两项完整产品能力：

1. 模型没有结构化、可审计、带来源的 Web 搜索与网页读取工具，无法可靠完成时效性任务。
2. MCP 只能在启动前手工配置；`/mcp` 只能查看，Agent 不能完成“解析配置 -> 权限确认 ->
   连通测试 -> 原子落盘 -> 明确生效状态”的闭环。

目标是让用户能用自然语言要求 Agent 查询网络或接入 MCP，同时继续遵守统一权限、密钥不落盘、
输出预算、恢复协议和模型后端零耦合。为控制风险和评审规模，拆成两个独立验收的里程碑：

- **M11b：可信联网检索**
- **M11c：扩展管理与运行产物治理（MCP + Skills）**

## 2. 调研结论

参考资料：

- [Claude Code MCP 文档](https://docs.anthropic.com/en/docs/claude-code/mcp)：使用显式 add/list/remove
  控制面管理 server，并区分配置作用域。
- [Codex MCP 文档](https://developers.openai.com/codex/mcp/)：CLI 与配置文件共同管理 MCP，连接信息不由
  模型暗中写入。
- [Gemini CLI MCP 文档](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md)：
  server 发现、schema 校验、工具注册、状态和信任是分离职责；支持 user/project scope、显式 trust、
  enable/disable 和集中诊断，密钥使用环境变量引用。
- [Playwright MCP 文档](https://github.com/microsoft/playwright-mcp)：产物目录、产物大小和输出模式可由
  `--output-dir`、`--output-max-size`、`--output-mode` 控制；默认目录不是客户端审计日志。
- [Agent Skills 规范](https://agentskills.io/)与主流 Agent 约定：用户安装的 Skill 和项目共享的 Skill
  应分 scope；项目 Skill 应位于可版本控制的专用目录，而不是混在被整体 gitignore 的运行状态目录。
- [MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)：外部 server
  元数据不应替代客户端权限判断。
- [GitHub REST Stargazers 文档](https://docs.github.com/en/rest/activity/starring)：在指定媒体类型下可取得
  单仓库 `starred_at`，但 GitHub Search API 没有“全站过去一周 Star 增长榜”字段。

可借鉴原则：

1. 搜索、抓取、GitHub API 是结构化工具，不以 Shell + curl 冒充正式能力。
2. 搜索结果必须携带 URL、标题、抓取/查询时间；回答层应区分来源事实与模型推断。
3. MCP 接入属于控制面变更，必须显式展示 command/args、URL、环境变量名和权限影响。
4. 先验证候选配置，再替换持久配置；失败不得破坏当前 Runtime 或原配置。
5. 密钥只允许环境变量引用，不接受写入 `config.yaml` 的明文 secret。
6. “扩展定义/安装”“Agent 审计日志”“第三方工具产物”是三类数据，必须分目录、分生命周期管理。

### 2.1 真实使用复盘

本次 Playwright MCP 试用暴露出三项确定缺陷：

1. 当前 `PermissionScope` 的 target 包含 `server/tool + 完整 args`。选择“本会话允许”后，只要点击
   目标、文件名等参数变化，就会再次询问；UI 文案与实际授权粒度不一致。
2. 一个 MCP 调用同时展示 `mcp.call` 与推测性的 `network.access`。对于 stdio server，客户端并不能
   约束或准确观察其内部联网行为，这条权限既重复又容易制造虚假的安全感。
3. `.playwright-mcp/page-*.yml` 是 Playwright 的页面快照产物。因为启动配置没有指定 output dir，
   server 按默认行为写入当前工作目录；它不是 Agent 为每个 MCP 建立的日志目录。

同时，当前 `./.assistant_agent/` 混放 sessions、runs、logs、artifacts、eval 输出和 Skills，并被整体
gitignore。这样既污染仓库工作目录，也使项目共享 Skill 无法正常版本控制，职责需要拆开。

## 3. 关键事实边界

“GitHub 上一周 Star 增长前十”不是官方 GitHub Search API 可直接、完整回答的查询：

- `stars` 排序是当前累计 Star，不是时间区间增量。
- `created:>=... sort:stars` 表示“一周内新建仓库的累计 Star 排名”，不是所有仓库周增量。
- 精确全站周增量需要持续快照、完整 Star 事件数据集或可信第三方历史服务。

因此 M11b 必须让 Agent明确数据口径和来源。没有可信历史数据时，可以提供“GitHub Trending 周榜”、
“一周内新建仓库按累计 Star 排序”或“第三方周增量榜”，但不得把它们表述为 GitHub 官方全站周增量。

## 4. M11b 范围：可信联网检索

### 4.1 必做

1. 新增 `web_search`：接收 query、结果数、可选 freshness；返回有界结构化结果（标题、URL、摘要、
   来源、查询时间）。
2. 新增 `fetch_url`：读取 HTTP(S) 页面并提取正文；返回最终 URL、标题、正文、内容类型、抓取时间和
   截断状态。
3. 新增 Web backend 抽象，搜索服务由配置选择，不把某个服务商写死到工具或 Agent 逻辑。
4. 提供至少一个无需密钥即可启用的 backend；付费/高配额 backend 的 key 只从环境变量读取。
5. 两个工具统一声明 `network.access`，沿用 Registry 的 deny -> ask -> allow、审计、输出预算和
   ToolDisplay。
6. `fetch_url` 实施应用层 SSRF 防护：仅 HTTP(S)、拒绝 URL 凭据、解析并拒绝 loopback/private/
   link-local/multicast/unspecified 地址、逐跳检查重定向、限制重定向次数/超时/响应字节/正文字符和
   可接受内容类型。
7. 网络错误归一为稳定错误码（超时、DNS、策略拒绝、HTTP 状态、内容类型、响应过大、解析失败），
   并标记是否可重试。
8. 系统提示词只增加简短使用原则：时效性问题先搜索，关键结论保留来源；不注入服务商专属提示。
9. 更新 `config.example.yaml` 和安装文档，默认配置可启动，未配置可选 key 时不泄漏或崩溃。

### 4.2 可选（M11b 验收后再决定）

- `github_search` 专用工具：封装 GitHub repositories search，支持可选 `GITHUB_TOKEN`、分页和限流
  元数据。它提高代码项目检索质量，但仍不伪造历史 Star 增量。
- 本地查询缓存：只缓存公开、非敏感查询；缓存键包含 backend 和参数，设置短 TTL 与容量上限。

### 4.3 本期不做

- 浏览器自动化、登录态网页、验证码绕过和 JavaScript 完整渲染。
- 自建搜索索引或爬虫。
- 声称应用层 URL 校验等同于 OS/网络沙箱；DNS rebinding 等 TOCTOU 风险仍登记边界。
- 未经来源支持生成精确 GitHub 全站周 Star 增量榜。

## 5. M11b 技术设计

新增职责建议：

```text
src/assistant_agent/web/
  client.py          # backend 协议、错误模型、SearchResult
  security.py        # URL 规范化、DNS/IP/重定向策略
  extract.py         # HTML 到有界正文
  backends/          # 可替换搜索 backend
src/assistant_agent/tools/
  web.py             # web_search / fetch_url Tool 适配
```

- `tools/web.py` 只处理工具 schema、权限声明和 ToolResult，不承载 HTTP 细节。
- Web client 不依赖 Agent、UI、Session 或 LLM provider。
- Runtime 从 `WebConfig` 构造 client 并注册工具；禁用时不注册、不产生网络副作用。
- HTTP 使用现有依赖能力，流式读取并在来源边界停止，不能先把无限响应完整读入内存再截断。
- 搜索结果正文进入模型前继续受单次/任务累计工具输出预算约束。

## 6. M11c 范围：扩展管理与运行产物治理

### 6.1 必做

1. 扩展控制面命令：`/mcp list`、`/mcp add`、`/mcp test`、`/mcp enable|disable`、
   `/mcp trust`、`/mcp remove`；保留无参 `/mcp`
   的兼容行为。
2. 新增模型可调用的 `configure_mcp_server` 工具，使自然语言任务能生成并提交候选 server 配置；
   工具不接受明文密钥，只接受环境变量引用。
3. 支持现有两种 transport：stdio（command + args + env 引用）和 Streamable HTTP（URL + header
   环境变量引用）。
4. 在落盘前对候选 server 做隔离探测：启动/连接、initialize、`tools/list`、schema 校验、工具数量
   上限和超时；输出 server 状态与发现的工具摘要。
5. 配置变更采用事务：读取结构化 YAML -> Pydantic 校验候选 AppConfig -> 临时文件原子替换；失败
   保留原文件。测试阶段不修改当前 registry。
6. 安装/配置阶段的权限至少覆盖 `process.execute`、`network.access`、`filesystem.write`；根据
   transport 和命令精确声明目标。用户确认界面展示脱敏后的完整 command/args 或 URL、将写入的配置
   scope、路径及生效方式。
7. 重做 MCP 调用授权粒度：参数只用于本次风险预览和审计，不进入 remembered scope。交互提供
   “允许一次 / 本会话允许此工具 / 本会话信任此 server / 拒绝”；后两者分别按 `server+tool` 和
   `server` 命中，不能因参数变化失效。
8. MCP 调用只生成一个聚合确认面板。`mcp.call` 是可执行的客户端边界；server 可能联网、读写或产生
   外部副作用作为风险说明展示，不再伪装成客户端能独立强制的 `network.access` 调用权限。HTTP
   server 的连接、MCP 安装下载仍在连接/配置阶段单独走真实网络权限。
9. 持久 trust 只能通过 `/mcp trust` 或配置控制面显式设置，可选 `tool`/`server` 粒度和 `user`/
   `project` scope；默认仍为 ask。server annotations 只做展示和风险升高，不能自行获得信任。
10. stdio 子进程使用最小环境：仅基础运行变量和配置中显式列出的变量；不把宿主全部 secret 自动
   传给第三方 server。
11. MCP manager 为 stdio server 注入受管 `cwd` 和 `ASSISTANT_AGENT_ARTIFACT_DIR`。对已知模板（首个为
    Playwright）额外注入官方支持的 output-dir/output-size 参数；通用 server 若不支持该环境变量，
    明确标记为“产物目录不可强制”，不虚假承诺。
12. remove 同样先确认并原子更新；删除配置不卸载全局 npm/pip 包，也不删除用户数据。操作完成后
    明确报告：已验证、配置 scope、发现工具数、运行目录和**下次启动生效**。

### 6.2 权限体验模型

MCP 权限分为三层，避免“安全”与“每一步弹窗”等价：

| 层级 | 何时确认 | 记忆键 | 生命周期 |
|------|----------|--------|----------|
| 安装/连接 | 下载包、启动进程、连接远端、写配置 | server + 动作 + scope | 单次事务 |
| 工具调用 | 未信任 server/tool 首次调用 | server/tool 或 server | 当前会话 |
| 持久信任 | 用户显式执行 `/mcp trust` | server/tool + user/project | 配置持久化 |

规则：deny 永远优先；高风险显式规则不能被会话 trust 覆盖；参数和结果始终进入脱敏审计，但不扩大或
缩小 remembered scope。`auto_approve: true` 迁移为显式 `trust: server`，保留兼容读取并给迁移提示。

Playwright 这类连续浏览任务的推荐体验是：首次调用时选择“本会话信任此 server”，随后点击、截图、
读取网络请求不再反复询问；退出 Agent 后该信任自动失效。只有用户明确持久 trust 才跨会话生效。

### 6.3 安装语义

第一版把“安装 MCP”定义为“建立一个可复现的 server 启动配置并验证可连接”：

- 推荐使用 `npx -y <package>@<version>`、`uvx <package>@<version>` 或明确的本地可执行文件，避免默认
  全局安装和不可追踪的环境修改。
- 若用户要求 `npm install -g`、`pip install` 等持久环境修改，仍通过 Shell 现有权限路径单独确认；
  `configure_mcp_server` 不偷偷执行包管理器全局安装。
- 未给版本时允许使用最新版，但确认中必须标注供应链漂移风险；文档示例推荐固定版本。

### 6.4 Skill scope 与安装目录

Skill 不再默认安装进项目运行状态目录。采用以下发现顺序：

```text
<workspace>/.agents/skills/<name>/SKILL.md     # project：项目共享、可提交 Git
~/.assistant_agent/skills/<name>/SKILL.md     # user：个人安装、跨项目复用
<config skills.dirs>                           # configured：显式附加目录
```

- “安装一个 Skill”默认写入 user scope；只有用户明确 `--scope project` 才写 `.agents/skills/`。
- project Skill 仍是不可信项目内容，首次进入 prompt 前走现有聚合授权；user Skill 视为用户主动安装。
- 旧 `./.assistant_agent/skills/` 只做一个版本周期的只读兼容发现，启动时提示迁移，不自动移动或删除。
- 增加 `/skills list|install|remove|doctor`，展示来源、scope、是否受信和冲突遮蔽关系。

### 6.5 本期不做：当前会话热注册

M11c 第一版不在进行中的 Agent turn 内替换工具 schema。原因是 `AgentLoop` 在构造时固定 schema，且
schema hash 同时参与上下文预算、Run checkpoint 与恢复兼容判断。工具执行中途改变 schema 会让同一
个 Run 的模型请求和恢复元数据不一致。

第一版已经完成“Agent 自己生成配置、请求授权、验证、落盘”的核心闭环，新增 MCP 工具在下次启动时
接入。后续只有出现明确体验收益时再单独设计任务边界热重载：候选 manager 完整启动后原子替换、重算
上下文预算、刷新恢复 hash，并禁止 Run 进行中切换。该工作可能需要修改内核，届时重新申请授权。

## 7. 配置与持久化

建议新增：

```yaml
web:
  enabled: true
  search:
    backend: <默认无密钥 backend>
    max_results: 10
  request_timeout: 15
  max_response_bytes: 2000000
  max_content_chars: 30000
  max_redirects: 5
```

### 7.1 目录职责

采用“安装目录、工作区状态、项目声明”三分法：

```text
~/.assistant_agent/
  skills/                              # user Skill 安装
  mcp/servers/<name>/                  # 受管 MCP manifest/lock，可选隔离运行环境
  workspaces/<workspace-id>/
    sessions/                          # 会话
    runs/                              # checkpoint
    logs/                              # Agent 统一 JSONL 审计
    artifacts/
      tools/                           # 内置工具大输出
      mcp/<server>/<session-id>/       # MCP 生成文件/快照/截图
    mcp-stderr/<server>/               # 有界、轮转的诊断 stderr，仅 debug/异常保留

<workspace>/.agents/skills/            # 可提交的 project Skill
<workspace>/config.yaml                # 当前项目私有配置，继续 gitignore
```

`workspace-id` 由规范化路径哈希和可读目录名组成，避免同名项目冲突。所有根目录允许由
`ASSISTANT_AGENT_HOME`/配置覆盖，测试不能写真实用户 HOME。

### 7.2 日志与产物不是一回事

- Agent 继续只维护统一 JSONL 审计流，一次 MCP 调用是一条带 `server/tool/call_id/session_id` 的事件；
  **不为每个 MCP 默认创建独立业务 log**。
- server stderr 是诊断数据：有界捕获、脱敏、轮转；正常时不刷屏，需要时由 `/mcp doctor <server>` 查看。
- screenshot、页面 snapshot、下载文件等是 artifact：按 workspace/server/session 归档，ToolResult 返回引用，
  配置保留数量/总字节/保留天数。
- 用户显式指定仓库内输出路径时尊重该路径并走文件写权限；未指定时使用受管 artifact 目录。

### 7.3 配置 scope 与事务

MCP server 定义支持 user/project 两种 scope，按“project 同名覆盖 user”的确定顺序合并，并在 `/mcp
list` 中显示最终来源。需要新增一个配置写入服务，供 init、slash 控制面和
`configure_mcp_server` 复用；禁止字符串拼接 YAML。写入采用 round-trip YAML + Pydantic 候选校验 +
原子替换，不能因添加一个 MCP 重排或抹掉用户整份配置注释。

## 8. 测试计划

### M11b

- Search backend contract：成功、空结果、超时、限流、坏 JSON、重复/非法 URL、结果上限。
- URL 安全：IPv4/IPv6 私网、localhost、userinfo、非 HTTP scheme、DNS 失败、重定向到私网、重定向环。
- Fetch：HTML、纯文本、错误内容类型、charset、gzip、有界流读取、正文与响应双重截断。
- 权限：readonly/strict/workspace/unrestricted，交互允许/拒绝，非交互默认拒绝路径。
- 展示：normal 一行摘要、verbose 有界详情、quiet 不泄漏过程信息，URL/token 脱敏。
- 端到端使用录制 HTTP fixture，不让 CI 依赖公网或搜索服务稳定性。

### M11c

- CLI 参数解析与无参兼容；add/test/remove 成功和错误提示。
- stdio/HTTP fake MCP：连接成功、超时、初始化失败、坏 schema、零工具、超上限、部分失败和资源清理。
- 权限矩阵：参数变化不触发已记住的 tool/server 会话授权；不同工具和 server 不越权；deny/显式 ask
  规则优先；跨会话只保留显式持久 trust。
- 确认 UI：一次调用只有一个聚合面板，不重复展示不可执行的推测能力；审计仍保留完整脱敏风险信息。
- secret：拒绝疑似明文 key；`${ENV_VAR}` 保留引用；确认、日志、ToolResult 全链路脱敏。
- 配置事务：候选验证失败、写入失败、原子替换失败均保留原文件；成功后可由 `load_config()` 重载。
- 目录：Windows/Linux HOME、同名 workspace hash、路径覆盖、目录逃逸、保留策略和并发创建。
- Playwright fixture：默认 snapshot/screenshot 进入受管 MCP artifact 目录，仓库根不出现
  `.playwright-mcp`；通用 server 不支持 output-dir 时报告真实边界。
- Skill：user/project/configured 优先级、冲突、旧目录兼容提示、安装 scope、项目 Skill 授权。
- Playwright MCP 生命周期：在隔离 `ASSISTANT_AGENT_HOME` 中通过 CLI 和模型工具各完成一次安装，验证
  manifest/config、连通和工具发现；执行浏览/快照后卸载，确认 server 配置、受管进程和受管安装目录
  已清理，其他 server、npm 全局缓存及历史 artifact 未被误删。另测显式 `--purge-artifacts` 的二次
  确认与定向清理。
- Skill 生命周期：从仓库内固定 fixture 分别安装到 user/project scope，验证发现、信任提示、
  `load_skill` 和同名优先级；随后卸载并确认只删除目标 scope，另一个 scope 自动恢复可见，非受管来源
  和其他 Skill 不受影响。旧目录仅兼容读取，不被卸载命令顺带删除。
- 恢复：配置工具 checkpoint 后恢复不自动重放；当前 Run 的 tool schema hash 不改变。
- 回归：现有 MCP manager、423 个测试、18 个 scripted eval、4 个 recovery eval 不退化。

## 9. 验收标准

### M11b

1. Agent 可在严格权限确认后完成普通时效性搜索、读取至少两个来源并在回答中给出可核验 URL。
2. 面对 GitHub 周 Star 问题会先声明指标口径；无历史数据时不伪造“官方周增长榜”。
3. 私网/localhost/危险重定向被阻止，超大或超时响应不会突破工具输出和内存边界设计。
4. 搜索 backend 可通过配置替换，不修改 Agent、工具或 LLM 业务逻辑。

### M11c

1. 用户可用自然语言或 `/mcp add` 提交 stdio/HTTP server；Agent 展示风险、取得授权、完成探测并
   原子写入配置。
2. 失败不会污染原配置、当前 registry 或遗留子进程；成功后重启 Agent 可发现新工具。
3. 配置、日志、终端和 Session 中均无明文密钥；未信任 server 调用继续经过统一权限门。
4. 删除 server 只移除配置，不误删用户软件或数据。
5. Playwright 连续浏览任务选择一次“本会话信任 server”后不再逐工具询问，其他 server 不被放行。
6. MCP 产物不落仓库根；统一审计、stderr 诊断和工具 artifact 可明确区分并可控清理。
7. 新安装个人 Skill 位于 `~/.assistant_agent/skills`，项目 Skill 位于可提交的 `.agents/skills`；旧目录
   可读且有迁移提示。
8. 在全新临时 HOME 中安装 Playwright MCP 后可真实连接并调用；卸载后重启不再发现该 server，且没有
   遗留子进程、配置项或仓库根产物。默认保留历史 artifact，只有显式 purge 才删除。
9. Skill 的 user/project 安装与卸载均可重复执行且结果幂等；卸载一个 scope 不影响另一个 scope、
   非受管 Skill 或用户其他文件。

两期共同 DoD：`pytest --cov`、架构测试、Ruff、mypy、scripted eval、recovery eval 全绿；技术债、
ROADMAP、README、AGENTS/CLAUDE 当前状态和实测数字同步。

## 10. 实施顺序

1. M11b-P1：Web DTO/backend contract、配置与错误模型。
2. M11b-P2：URL 安全、流式 fetch 与正文提取。
3. M11b-P3：web_search/fetch_url、权限、展示、提示词和文档。
4. M11b-P4：全量验收、状态同步、方案归档。
5. M11c-P1：统一 home/workspace state locator、Skill scope 与旧目录兼容。
6. M11c-P2：MCP 授权 scope、聚合确认、trust 迁移和回归测试。
7. M11c-P3：MCP 配置事务、候选探测、受管 cwd/artifact/stderr。
8. M11c-P4：`configure_mcp_server` 与 `/mcp list|add|test|enable|disable|trust|remove`。
9. M11c-P5：Playwright MCP 安装 -> 连接 -> 调用 -> 卸载真实闭环；Skill user/project 安装 -> 加载 ->
   卸载闭环；残留/幂等检查、全量 DoD、状态同步和方案归档。

## 11. 风险与决策门

- **搜索服务稳定性**：通过 backend contract 隔离；CI 使用 fixture；运行时错误明确可重试性。
- **网页提示注入**：网页是工具数据而非指令；提示词标注外部内容不可信，ToolResult 保留来源边界。
- **SSRF 不是沙箱**：应用层校验降低常见风险，但不宣称阻止所有 DNS/代理/内核层绕过。
- **MCP 供应链风险**：确认中展示包名、版本和命令；默认不全局安装、不继承所有环境变量。
- **server 产物不可完全控制**：仅能约束 cwd、环境变量和已知模板参数；恶意或不遵守约定的 server
  仍需 OS 沙箱解决，客户端不能声称已隔离。
- **目录迁移破坏历史**：首版采用新目录写入、旧目录只读兼容；不自动搬迁，不删除用户现有数据。
- **卸载误删**：只删除安装 manifest 记录为 owned 的路径；默认不删历史 artifact、包管理器全局缓存或
  用户手写目录。purge 使用独立确认并再次校验路径归属。
- **持久 trust 扩大风险**：只允许显式控制面设置，配置和启动 banner 明示；推荐会话 trust。
- **YAML 注释保留**：先用真实配置 fixture 评估 round-trip；若依赖代价过高，必须在实施前明确格式化
  影响，不能静默重写用户配置。
- **范围控制**：先完成 M11b 并验收，再进入 M11c；M11c 不夹带 OAuth、MCP resources/prompts、
  marketplace 或当前会话热重载。

## 12. 审阅门

用户确认本方案后才开始 M11b 实现。M11b 验收完成后再开始 M11c；本次确认允许修改 `web/`、
`tools/`、`config/`、`cli/`、`mcp/`、Runtime 装配、提示词、测试和状态文档，**不包含修改
`agent/loop.py` 的授权**。若实施中发现必须修改内核，立即停在方案层重新确认。
