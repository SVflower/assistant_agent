# M4 实施方案 — 工具集扩展

> 目标：补齐"理解代码 → 改动 → 验证变更"真实开发闭环里缺的两环——代码检索、变更查看。
> 状态：待审阅，未动代码。对应 [ROADMAP.md](../ROADMAP.md) M4。
> 最后更新：2026-07-02

---

## 一、结论

**该做工具集扩展。** 当前只有读/写文件、列目录、shell 四件套；做真实开发任务时，
理解代码靠 shell 拼 grep（Windows 无 grep）、看变更靠 shell 拼 git——不跨平台、
每次撞危险命令确认、拿不到结构化结果。M4 补上**代码检索（理解）**与**变更查看（验证）**两环。

工具体系（`Tool` ABC + `ToolRegistry` + `ToolContext.request_confirm(category)`）已为扩展就绪，
加工具是纯 `tools/` 扩展，**不动内核**。范围严格克制：只做撑起闭环的只读工具。

## 二、参考借鉴（成熟产品原则）

- **专用只读工具 > 让模型拼 shell**：Claude Code 明确偏好 Read/Grep 而非 cat/grep，
  理由是"更好的权限粒度和更清晰的抽象"——也解决跨平台与结构化。
- **只读自动放行，写/网络才确认**：Claude Code 自动放行 Read/Grep；Codex 权限与批准分离。
- **变更必须透明可 review**：Codex/Copilot 的核心是"改动可 diff、人来把关"，git diff 是抓手。
- **网络默认关**：Codex `workspace-write` 下网络仍默认关闭——佐证我们暂缓 HTTP。
- **保守默认**：错误代价高处从严。
- 不适合我们现阶段：云端隔离沙箱、PR 工作流、多档 sandbox——重量级设施，单人本地 CLI 用不上。

来源（搜索快照+二手技术文档综合；官方站点因网络限制未逐字核对，核心原则可信）：
Claude Code tools/permissions 文档、Codex approvals/sandboxing 文档、GitHub Copilot coding agent 文档。

## 三、当前项目评估

| 问题 | 结论 | 依据 |
|------|------|------|
| 工具体系适合扩展？ | ✅ 非常适合 | Tool ABC + registry.register + build_default_registry |
| 新工具放哪？ | `tools/` 新增文件 | `tools/search.py`、`tools/git.py`，在 registry 注册 |
| 需要调整注册机制？ | ❌ 不需要 | registry.execute 已统一异常兜底 |
| 需要新增 Tool 接口规范？ | ❌ 基本不需要 | 现 ABC 足够；写工具才需"需确认"声明点，M4 只读不需要 |
| 需要 ToolResult/ToolError/PermissionPolicy？ | ❌ 都不需要 | ToolResult 够用；request_confirm(category)+always_allowed 即 PermissionPolicy |
| 需要改核心？ | ❌ 不需要 | loop.run 对工具无知，加只读工具不碰 loop（守铁律4） |

**关键约束**：
- grep **必须纯 Python 实现**（pathlib 遍历 + re），不 shell 到系统 grep——否则 Windows 不可用。
- 现权限"危险判定"硬编码在 shell.py，是 shell 专属；M4 只读工具不需确认，此局限不构成问题。
  将来做写工具时才需把"需确认"提升为 Tool 基类声明方法（YAGNI，M4 不做）。

## 四、推荐范围

| 工具 | 分类 | 理由 |
|------|:---:|------|
| grep / code_search | ✅ 必做 | 理解代码基础；纯 Python 跨平台；只读无风险 |
| git（只读 status/diff/log/show/branch） | ✅ 必做 | 变更透明、验收/回滚前提；只读安全 |
| glob / find_files | 🟡 可选 | 补充检索，成本低 |
| web_fetch / 网络搜索 | ⏸ 暂缓（仅设计/受限 MVP） | SSRF/外泄/注入边界不成熟；网络默认关 |
| git 写（commit/reset/push） | ❌ 不做 | 破坏性/不可逆，超出只读优先 |
| 任意网络 POST / 装依赖 | ❌ 不做 | 高危，无沙箱兜底 |

### 为什么做这些工具
- **grep/code_search**：模型上下文装不下整个库、且 lost-in-the-middle；需"按需检索"而非"全量灌入"。
  用法：改 X → 先 grep 定位定义与调用点 → 精准读改。
- **git 只读**：让 agent 理解工作区（改了什么/与上次差异/历史）；diff 让用户 review 后再接受；
  log 提供回滚锚点。只读优先：读零风险收益立现，写不可逆先不给。
- **HTTP**：适合查文档/取数据；风险 SSRF、数据外泄、内容注入。当前暂缓；若做只受限 MVP
  （仅 GET、拦私网、大小/超时上限、每次确认、抓回内容标注不可信）。

## 五、技术设计

### grep（code_search）
```
input:
  pattern: string          # 必填，正则
  path: string = "."
  glob: string | null      # 如 "*.py"
  ignore_case: bool = false
  max_results: int = 100
output: 多行 "相对路径:行号: 匹配行"（截断+超出提示）；无匹配 → ok("未找到匹配")
默认跳过 .git/.venv/__pycache__/node_modules。纯 Python，跨平台。
```

### git（只读，单工具 + 子命令白名单）
```
input:
  subcommand: enum["status","diff","log","show","branch"]   # 白名单
  args: string = ""        # 附加参数，需清洗
output: stdout/stderr + 退出码（复用 shell 的 _decode、stdin=DEVNULL）
        非零退出 → ok(输出)，交模型判断
安全：白名单强制只读；写子命令直接 error 拒绝；args 严格清洗防 shell 逃逸。
```
> 设计：单 git 工具 + 白名单，优于 3 个独立工具——少 schema、只读集中强制、易审计。

### web_fetch（暂缓；MVP 设计）
```
input: url: string
output: 抓回正文（截断），带 "[外部不可信内容]" 前缀
安全：仅 http(s)；拒绝 localhost/私网/元数据 IP；大小+超时上限；
     经 request_confirm("web_fetch", ...) 确认后才发。
```

### 权限策略
- grep、git-read：只读，**不确认**（对齐 Claude Code 自动放行）。
- web_fetch（若做）：每次确认，category=web_fetch，复用 request_confirm + always_allowed。
- 复用现有机制，**不新增 PermissionPolicy 抽象**。

### 错误处理
- 沿用现有：工具内部自处理异常返回 ToolResult.error；registry.execute 兜底。
- 输入校验失败 → ToolResult.error（清晰说明）。
- 命令非零退出不算工具错误（同 shell），输出交模型判断。

### 流式事件展示
- **无需新增事件类型**。走现有 tool_call / tool_result，Console 已渲染。
- git diff 较长 → 依赖现有 500 字预览截断，完整结果仍在上下文。

## 六、开发计划（分步，每步带测试）
1. grep/code_search（`tools/search.py`）+ 注册 → 单测：命中/忽略大小写/glob/无匹配/跳过忽略目录/截断
2. git 只读（`tools/git.py`）+ 注册 → 单测：status/diff/log 输出、写子命令被拒、非 git 目录（tmp_path 建临时 repo）
3.（可选）glob/find_files → 单测：按名匹配
4. 文档同步：README 工具列表、ROADMAP M4、TECH_DEBT（如有新债）
5. web_fetch：本期只出设计，不实现（或受限 MVP 单独评估）
6. 全程守 DoD：pytest + ruff + 架构测试全绿（新工具在 tools 层，不得反向依赖 agent/ui）

## 七、验收标准
1. code_search 能按模式找到代码，结果含 `文件:行号`，Windows 可用（单测覆盖命中/大小写/glob/无匹配/截断）
2. git 工具返回 status/diff/log；写子命令被拒绝（单测覆盖白名单+拒绝）
3. 只读工具不触发确认；（若做 web）网络工具必确认
4. 能完成真实闭环："搜索函数 → 读取 → 改动 → git diff 确认"（手动集成验证）
5. 新工具全部有单测；现有测试不回退；ruff + 架构测试通过
6. 内核 loop.py 未改

## 八、风险与边界（不能做过头）
- ❌ 不做 git 写操作（不可逆）——白名单机制挡死
- ❌ 不引入重框架（沙箱引擎/PR 工作流/多档权限）——复用 Tool/Registry/request_confirm
- ⏸ HTTP 默认不做——边界未成熟；要做只受限 MVP + 强确认 + 私网拦截
- ⚠️ grep 必须纯 Python（Windows 无 grep）
- ⚠️ git 工具别变 shell 逃逸口——args 清洗、子命令严格白名单
- ⚠️ 抓回的网络内容视为不可信数据，不当指令执行

## 九、不确定标注
Claude Code / Codex 官方文档站因网络限制无法直接抓取，本方案基于搜索快照 + 多篇二手技术文档
综合，核心原则可信；个别产品具体参数（如某工具是否 100% 自动放行）以官方为准，未逐字核对。
