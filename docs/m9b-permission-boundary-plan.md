# M9b 计划：统一权限与信任边界

> 状态：待用户审阅。上位规划见 `docs/phase3-trustworthy-agent-plan.md`。
> 本里程碑原则上不修改 `agent/loop.py`。

## 1. 目标

把当前“各 Tool 自觉询问”的提示机制升级为 Registry 强制执行的统一权限边界：每次工具调用
必须先声明动作，再经过同一套 `deny -> ask -> allow` 策略；项目 Skill、MCP 元数据和任意 Shell
都按不可信输入处理。M9b 提供可审计的应用层最小权限控制，但不冒充 OS 级沙箱。

## 2. 调研结论

调研以官方文档为主，提炼原则而非照搬 API：

1. [Claude Code permissions](https://code.claude.com/docs/en/permissions.md)：权限由框架而非模型提示词
   执行；规则优先级固定为 deny、ask、allow；永久允许必须按工具和目标范围收窄。
2. [Claude Agent SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions)：要覆盖每次工具
   调用，应在 `PreToolUse` 强制门控；普通 allow 回调不能跳过 deny/ask。
3. [OpenAI Codex approvals and security](https://developers.openai.com/codex/agent-approvals-security)：
   sandbox 是技术边界，approval 是越界决策，两者不能混为一谈；网络与文件系统应是独立维度。
4. [OpenAI Agents SDK tool guardrails](https://openai.github.io/openai-agents-python/guardrails/)：工具输入
   guardrail 必须包住每次调用，并能“拒绝但把稳定反馈交还模型”或终止运行。
5. [LangChain HITL middleware](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)：审批应是
   工具执行中间件；拒绝是失败结果，不能伪装成成功响应。持久化暂停属于 M10b，不在本期偷做。
6. [MCP Tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)：客户端应
   清楚展示工具暴露与调用，并保留人工拒绝能力。MCP annotations 只是 hint，未信任 server 的
   `readOnlyHint` / `destructiveHint` / `openWorldHint` 不能作为放行依据。

可借鉴的共同原则：权限是确定性运行时机制；默认最小权限；拒绝优先；目标范围必须进入授权键；
外部描述和模型输出都不是安全证据；审批与 OS 隔离必须在 UI 和文档中明确区分。

## 3. 现状评估

### 已有可复用基础

- `ToolRegistry.execute()` 是所有内置工具和 MCP 工具的统一执行入口，适合做强制门控。
- `ToolContext` 已持有 workspace、confirm 回调、会话状态、logger 和预算，无需改 AgentLoop。
- logger 已有确认耗时和工具 I/O 脱敏，可扩展权限决策事件。
- 所有工具都有结构化参数 schema，路径类工具可在执行前规范化目标。

### 当前缺陷

- `write/edit/multi_edit/shell/MCP` 分别自行确认，新增 Tool 忘记确认即可绕过。
- `read_file` 的工作区外读取不确认；Shell 只靠危险正则，解释器、脚本、别名和组合命令可绕过。
- `always_allowed` 只按粗 category，不能限制 capability、目标或命令范围。
- `confirm_dangerous_shell=false` 可直接关闭边界，且提示词错误声称联网和安装依赖都会自动拦截。
- 项目 Skill 的 name/description/body 可在未信任时进入系统上下文；MCP 描述与 annotations 同样来自外部。
- `auto_approve` 只表达跳过弹窗，没有把“信任整个 server”及其风险呈现给用户。

结论：现有分层适合扩展；新增 `tools/policy.py`、`tools/permissions.py` 和 observer 接口即可，
不需要修改 `agent/loop.py`，也不需要把权限逻辑复制到每个 Tool 的 `run()`。

## 4. 范围

### 必做

1. Registry 在预算消费和 Tool.run 之前强制收集权限请求并决策；Tool 无法绕过。
2. 定义 capability、请求、规则、决策、作用域键和稳定拒绝结果。
3. 实现 `readonly`、`workspace`（默认）、`strict`、`unrestricted` 四种模式。
4. 工作区内外读写分开；敏感路径默认 deny；网络、进程、MCP、Skill 是独立 capability。
5. Shell 采用严格只读 allowlist；其他命令保守声明广泛能力，拒绝等价写法绕过。
6. MCP 参数确认脱敏；未信任 server 的 annotations 不降低权限；`auto_approve` 明确迁移为 server trust。
7. 项目 Skill 未经会话确认或配置 trust 不进入系统提示词；个人 Skill 与项目 Skill 显示来源。
8. 增加同步 `PreToolUse` / `PostToolUse` observer 接口，支持阻断、审计和后续 guardrail 扩展。
9. 修正提示词、banner、README 和配置样例，明确当前保护等级及“无 OS 级真沙箱”。

### 可选（有余量才做）

- `/permissions` 只读命令，展示当前 mode、会话授权和规则来源；不做交互式规则编辑器。
- 对权限拒绝结果附稳定机器码，为 M9c eval 直接断言。

### 不做

- 不实现 Windows/macOS/Linux OS 沙箱、容器或网络代理。
- 不解析任意 Shell 为完整 AST，也不声称能准确判断脚本真实副作用。
- 不做 LLM prompt-injection 分类器或组织级远程策略中心。
- 不做跨进程审批恢复；pending approval 的持久化归 M10b。
- 不允许 observer 修改工具参数；M9b 只允许继续或拒绝，避免审计对象与执行对象不一致。

## 5. 权限模型

### 5.1 Capability

```text
filesystem.read
filesystem.write
process.execute
network.access
mcp.call
skill.load
user.interaction
```

路径读写必须带规范化绝对目标；网络带域名或 `unknown`；进程带完整命令摘要；MCP 带 server/tool；
Skill 带 source/name。一个调用可产生多个请求，最终结果采用最严格合并：任一 deny 即 deny，
否则任一 ask 即 ask，全部 allow 才执行。

### 5.2 数据结构

- `PermissionRequest(tool, capability, target, risk, category, metadata)`：不可变动作声明。
- `PermissionRule(effect, capability, target_pattern, tool?)`：配置规则，effect 为 deny/ask/allow。
- `PermissionDecision(effect, reason, matched_rule?, remember_scope?)`：可审计结果。
- `PermissionScope(capability, target, tool)`：会话“永久允许”的精确键。

规则按 `deny -> ask -> allow` 三组执行；同组按配置顺序首个匹配。模式只提供基线规则，用户规则叠加，
但 deny 永远不能被 allow 覆盖。路径先 `resolve()` 再按 workspace/sensitive roots 匹配，glob 仅匹配
规范化目标，不直接匹配模型原始字符串。

### 5.3 四种模式

| 模式 | 默认行为 |
|------|----------|
| `readonly` | workspace 内文件读取 allow；写、任意 Shell、网络、MCP 副作用 deny |
| `workspace` | workspace 内内置读写 allow；区外读写 ask；严格只读命令 allow；其余 Shell ask；MCP/项目 Skill ask |
| `strict` | 内置 workspace 读取 allow；写、Shell、网络、MCP、项目 Skill一律 ask，非交互时 deny |
| `unrestricted` | 默认 allow，但显式 deny 与敏感路径 deny 仍优先；banner 显示高风险 |

`workspace` 是推荐默认。非交互运行无法询问时，ask 稳定降级为 deny，不能自动放行。

### 5.4 Shell 的保守策略

不继续维护“危险命令黑名单”。新增 `ShellPermissionAnalyzer`：

1. 只接受无重定向、管道、命令替换、控制运算符、脚本文件和环境注入的单命令。
2. 仅对有限严格语法标为只读，例如 `git status/diff/log/show`、`pytest --collect-only`、
   `python --version` 等；命令与参数都需通过 allowlist grammar。
3. 所有其他命令声明 `process.execute + filesystem.write + network.access`。这是保守能力上界，
   因而 `python -c`、PowerShell、curl/pip、重定向、脚本和组合命令都不能静默绕过任一策略维度。
4. 用户“永久允许”只记忆规范化后的 exact command hash 和 capability 集，不扩散到整个 Shell。

该方案会增加部分确认次数，但比尝试证明任意 Shell 安全更诚实。真正限制已批准进程的系统调用仍需
OS sandbox；UI 必须明确这一边界。

## 6. Tool 与 Registry 契约

在 `Tool` 增加默认方法：

```python
def permission_requests(self, args: dict[str, Any], ctx: ToolContext) -> list[PermissionRequest]:
    ...
```

默认对未知第三方 Tool 返回一个 `ask` 倾向的 `process.execute/unknown` 请求，不允许“未声明即放行”。
无副作用工具显式返回空列表。内置 Tool 各自只负责从 args 声明目标，不负责弹窗或记忆。

Registry 顺序固定为：

```text
查找 Tool -> 参数基本校验 -> 收集请求 -> PreToolUse observers -> Policy 决策/确认
-> 消费工具调用预算 -> Tool.run -> 截断/累计输出 -> PostToolUse observers -> logger
```

权限拒绝不消费执行调用额度，返回 `executed=False` 的稳定 ToolResult；observer 异常默认 fail-closed，
并记录原因。Post observer 不能把失败改成成功。

## 7. 内置工具迁移

- `read_file/list_dir/code_search`：声明 `filesystem.read`；工作区外由策略 ask/deny。
- `write_file/edit_file/multi_edit`：声明 `filesystem.write`；删除现有 Tool 内确认，统一由 Registry 执行。
- `git`：已限制只读 subcommand，声明 `process.execute` 的可信内置只读 scope。
- `run_shell`：使用 Shell analyzer 产生一个或多个 capability 请求，删除危险正则作为授权依据；
  正则可保留仅用于风险说明，不能决定 allow。
- `ask_user`：声明 `user.interaction`，默认 allow。
- `load_skill`：声明 `skill.load(source/name)`；正文只在受信后返回。
- MCP Tool：声明 `mcp.call(server/tool)`；未信任 server 同时按保守上界声明 open-world/destructive；
  确认正文只展示递归脱敏和限长后的关键参数。

## 8. Skill 与 MCP 信任

### Skill

- `SkillMeta` 增加 `source`（project/personal/configured）和 `trusted`。
- 个人目录默认 trusted；项目目录默认 untrusted；自定义目录必须在配置中显式指定 trust。
- 构造系统提示词前，对项目 Skill 做一次聚合会话确认；拒绝或非交互时完全不注入 name、description、body。
- `/skills` 显示来源和信任状态。项目 skill name 使用严格格式，description 限长、去控制字符。
- 配置 `skills.trusted_project_skills` 支持按规范化路径或名称显式 trust；不自动改写配置。

### MCP

- `auto_approve=true` 迁移语义明确为“信任该 server 的全部当前工具”，启动时输出高风险提示。
- 未信任 server 的 annotations 仅用于 UI 风险补充，绝不能把 ask/deny 降级为 allow。
- server/tool/参数都进入 permission category；会话记忆不跨 server 或 tool。
- 参数使用 logger 现有递归脱敏能力的公共 helper，避免权限 UI 与日志各写一套不一致脱敏。

## 9. 配置与兼容

新增：

```yaml
permissions:
  mode: workspace
  rules: []
  sensitive_paths:
    - "~/.ssh/**"
    - "~/.aws/**"
skills:
  trusted_project_skills: []
```

- 未配置时启用 `workspace`，这是有意的安全收紧。
- `tools.confirm_dangerous_shell` 标记 deprecated；不再能关闭 Registry 权限边界。
- 旧值 `false` 加载时给清晰迁移警告，用户若确实需要宽松行为应显式设置
  `permissions.mode: unrestricted`，避免旧布尔值静默变成全权限。
- `mcp.auto_approve` 暂保留兼容字段，但 README/config 注释改为“信任整个 server”，M10 之后再评估改名。

## 10. Observer 与审计

新增同步接口：

- `PreToolUse.on_pre_tool(requests, args) -> allow | deny`：可附拒绝原因，运行在 Policy 前；deny 后不执行。
- `PostToolUse.on_post_tool(requests, result)`：只观察，异常记录但不覆盖原结果。

logger 新增 `permission_decision`，字段包含 mode、tool、capabilities、脱敏 targets、decision、reason、
remembered、matched_rule；原 `confirm` 事件保留一版兼容后逐步淘汰。任何日志都不记录原始密钥参数。

## 11. 文件改动

预计新增：

- `tools/permissions.py`：数据结构、capability、scope。
- `tools/policy.py`：模式、规则匹配、决策合并、会话记忆。
- `tools/shell_policy.py`：严格只读 grammar 与保守 capability 分析。
- `tools/observers.py`：Pre/Post observer 协议与组合执行。
- `tests/test_permissions.py`、`tests/test_shell_policy.py`。

预计修改：

- `tools/base.py`、`tools/registry.py` 与各内置 Tool：动作声明和统一门控。
- `config/schema.py`、`config/loader.py`、`config.example.yaml`：模式、规则与旧配置迁移。
- `mcp/tool.py`、`mcp/manager.py`：server trust、annotations、参数脱敏确认。
- `skills/store.py`、`skills/tool.py`、`cli/setup.py`：来源标记和注入前信任门。
- `obs/logger.py`、`ui/console.py`、`ui/formatting.py`：审计与保护等级展示。
- `agent/prompts.py`、README/INSTALL：删除虚假沙箱承诺，说明应用层权限边界。

明确不修改 `agent/loop.py`。若实现中发现必须修改，停止并重新向用户确认。

## 12. 测试计划

### 策略单测

- deny/ask/allow 优先级；模式基线；规则顺序；非交互 ask=>deny。
- 会话永久允许按 capability + target + tool 精确隔离，不跨路径、域名、命令、server/tool。
- 路径规范化覆盖 `..`、绝对路径、符号链接（平台支持时）和大小写差异。
- observer deny、observer 异常 fail-closed、Post 异常不改执行结果。

### 绕过回归

- Python `-c`、PowerShell、cmd、shell 脚本、重定向、管道、命令替换均不能进入只读 allowlist。
- curl/wget/pip/npm、解释器内联网与未知脚本都至少声明 network + process，并受 deny 阻断。
- 区外 read/write、敏感目录 read/write 分别有独立决策。
- 工具自己不调用 confirm 时 Registry 仍门控；未知第三方 Tool 默认 ask。

### Skill/MCP/UI

- 未信任项目 Skill 的任何 metadata/body 不进入 system prompt；确认后才注入；非交互默认排除。
- MCP 参数递归脱敏且限长；annotations 不可信时不降低权限；auto_approve 显示 server 级信任警告。
- banner、README、prompt 明确“应用层权限、无 OS 沙箱”；不同 mode 显示正确保护等级。
- 旧配置加载有迁移提示，不静默关闭权限。

### 集成与回归

- Registry 预算、确认等待计时、日志、批次协议与 ToolResult 兼容测试不回退。
- `pytest`、`pytest --cov`、架构测试、`ruff format --check .`、`ruff check .`、mypy 全绿。

## 13. 验收标准

1. 所有 Tool 调用必经 Registry 权限门；没有 Tool 可通过漏写 confirm 绕过。
2. Python/PowerShell/重定向/curl/pip/未知脚本不会被误判为自动允许的只读命令。
3. 区外读、区外写、网络、进程、MCP、Skill 有独立 capability、规则和审计事件。
4. deny 永远优先；会话“永久允许”不扩散到不同目标或 capability。
5. 未信任项目 Skill 不进入 prompt；未信任 MCP annotations 不作为放行证据；参数确认已脱敏。
6. 默认 `workspace` 模式可完成正常编码任务；更严格/宽松模式行为与文档一致。
7. UI 和文档明确当前没有 OS 级真沙箱，不制造虚假安全感。
8. 不修改 `agent/loop.py`；M9a 的 258 测试基线不回退，新增关键路径测试完整。

## 14. 风险与控制

- **确认次数上升**：严格只读 grammar + 精确会话记忆降低疲劳，不扩大授权范围换便利。
- **Shell 能力保守过度**：这是无 OS sandbox 下的有意取舍；允许用户对 exact command 明确授权。
- **配置迁移改变行为**：启动提示和 README 明示，旧布尔值不静默映射到不受限模式。
- **Skill 可发现性下降**：项目 Skill 在会话启动时聚合确认，拒绝后可由 `/skills` 查看，不把不可信
  描述塞进系统提示词换便利。
- **MCP annotations 不可靠**：只有配置明确 trusted server 后才可用于降低提示；默认按最坏情况处理。
- **权限不等于沙箱**：已批准进程仍拥有宿主进程权限；真正硬隔离列入未来容器/WSL2/VM 方向。

## 15. 实施拆分

1. P1：权限数据模型、模式、规则、会话 scope 和 observer，先用假 Tool 测 Registry 强制门控。
2. P2：迁移内置文件/Git/Shell/ask 工具，删除分散确认，完成绕过回归。
3. P3：Skill/MCP 信任来源、参数脱敏与配置迁移。
4. P4：UI/prompt/docs、审计事件、全量验收、技术债更新、状态同步和归档提交。

每步保持可测试；P1 完成前不删除旧 Tool 内确认，P2 统一切换后再移除，避免中间状态无保护。
