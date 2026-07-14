# M7a — Agent Skills 系统（第二阶段·生态接入其一）

> 里程碑正式计划。开工前评审锚点，收尾对照验收。
> 前置调研：本会话对 Claude Code / Codex / Anthropic Agent Skills（SKILL.md + 渐进披露）的实现调查。
> 关联：M7b（MCP client）复用本期搭好的"动态 prompt 注入足场"。

## 解决什么

agent 目前的能力全靠内置工具 + 一段静态系统提示词。要复用"针对某类任务的做法手册"（怎么跑本项目测试、怎么发布、某领域的处理套路），只能塞进提示词或每次重述。**Skill = 可复用的指示书**：一个装着 `SKILL.md`（+ 可选脚本/参考文件）的文件夹，模型按需加载、按其指示用**现有工具**完成任务。

Skill 不是工具、不是 MCP：它是**注入提示词的知识**，靠渐进披露控制上下文占用。

## 范围（本期做 / 不做）

**做：**
- `skills/` 新层：扫描 `./.assistant_agent/skills/` 与 `~/.assistant_agent/skills/`，解析 `SKILL.md` frontmatter。
- 渐进披露两级：Level 1（`name`/`description` 注入系统提示词，常驻）；Level 2（`load_skill(name)` 工具按需返回正文）。
- Level 3（脚本/参考文件）不做新机制——正文指向它们，模型用现有 read_file/run_shell 读或跑。
- `SkillsConfig` 配置（enabled + 目录可覆盖）；`/skills` slash 命令列出可用技能。

**不做（本期）：**
- MCP（M7b）。
- 远程拉取/技能市场（供应链风险，信号驱动）。
- 强制沙箱执行技能脚本——沿用既有危险确认门，不新造。
- 技能热重载（listChanged 式）——启动时扫描一次即可。

## 架构：新增 skills/ 层，依赖单向

```
main._setup ──组装──┐
                    ├─→ SkillStore.discover(dirs)   → 得到 [SkillMeta]
                    ├─→ build_system_prompt(interactive, skills=metas)  → 注入 Level 1
                    ├─→ registry.register(LoadSkillTool(store))         → Level 2 工具
                    └─→ AgentLoop(..., system_prompt=注入后的提示词)
```

- **层级**：`skills/` 定为 rank 2（与 tools 同级，叶子能力层）。只依赖 `tools/base`（LoadSkillTool 继承 Tool）。不依赖 agent/ui/cli/main。登记进 `tests/test_architecture.py` 的 `_LAYER_RANK`。
- **依赖注入而非反向依赖**：`skills/` 不 import `config`——目录列表由 main 以参数传入（`discover(dirs)`），保持 skills 层纯净、可单测。
- **registry / loop 不认识 skills**：LoadSkillTool 就是普通 Tool，走 `registry.execute()`，自动被 M6 审计 / M6.5 预算 / 危险确认罩住。

## 关键设计点

**1. 渐进披露落地**
- Level 1：`SkillMeta{name, description, path}`，只取 frontmatter。每技能几十 token。注入系统提示词"# 可用技能"节：`- name：description`。
- Level 2：模型调 `load_skill(name)` → 读该技能 `SKILL.md` 正文（去掉 frontmatter）用 ToolResult 返回。
- Level 3：正文里写"运行 scripts/x.py""先读 reference/y.md"——模型用现有工具执行。零新机制。

**2. system prompt 注入（复用现成接缝）**
- `Conversation.__init__` 已有 `system_prompt` 参数（context.py:40），只是 AgentLoop 没传。
- `build_system_prompt(interactive, skills=None)` 加可选参数：有技能则追加"# 可用技能"节。
- main 构造增强提示词字符串 → 传给 `AgentLoop(system_prompt=...)` → 转发给 Conversation。
- **存活性**：`/clear` 只 `load_history([])`、`/model` 只换 client，都不动 `_system`——注入一次天然存活，无需重注。

**3. 内核触碰（需说明）**
- `agent/loop.py`：`AgentLoop.__init__` 加一个可选参数 `system_prompt: str | None = None`，原样转发给 `Conversation`。**不改 run() 控制流、不改任何循环逻辑**，只是补齐一条已存在于 Conversation 的构造参数的透传。风险等同 M4.5 的 set_client（轻碰、控制流不变）。开工时确认现有测试不回退。

**4. 安全**
- 技能脚本经现有 run_shell → 自动走危险确认门（M4.7 工作区范围 + 确认）。
- frontmatter description 会进系统提示词（提示注入面）——本期只扫本地目录、不远程拉取，等同"用户自己放进来的文件"，风险可控；文档提示"第三方技能需代码审查，与加依赖同级"。
- 技能名 → 文件路径：`load_skill(name)` 只在已发现的 SkillMeta 里按名查，不接受任意路径，杜绝路径穿越。

## 配置

```yaml
skills:
  enabled: true
  # 留空用默认：./.assistant_agent/skills/ 与 ~/.assistant_agent/skills/
  dirs: []
```

## SKILL.md 格式（Claude 兼容）

```markdown
---
name: run-tests
description: 如何在本项目跑测试与 lint。当用户要验证改动、跑测试或检查 ruff 时使用。
---

# 跑测试
1. 激活 venv 后执行 `pytest -q`。
2. lint：`ruff check src tests`。
...
```

## 文件改动清单

**新增：**
- `src/assistant_agent/skills/__init__.py`
- `src/assistant_agent/skills/store.py`：`SkillMeta`、`SkillStore`（discover / list / get_body）、frontmatter 解析。
- `src/assistant_agent/skills/tool.py`：`LoadSkillTool(Tool)`。
- `tests/test_skills.py`：解析/发现/加载/注入/边界。

**修改：**
- `agent/prompts.py`：`build_system_prompt(interactive, skills=None)` + 技能节渲染。
- `agent/loop.py`：`AgentLoop.__init__` 加 `system_prompt` 透传（内核轻碰）。
- `config/schema.py`：`SkillsConfig` + 挂到 `AppConfig`。
- `main.py`：`_setup` 里发现技能→注入→注册 LoadSkillTool→传 system_prompt。
- `cli/commands.py`：`/skills` 命令。
- `tests/test_architecture.py`：`_LAYER_RANK` 加 `skills: 2`。
- `config.example.yaml`：skills 段示例。

## 测试计划（关键路径）

1. **frontmatter 解析**：正常 / 缺 name / 缺 description / 无 frontmatter / 空文件 → 分别正确或跳过并不崩。
2. **发现**：多目录合并、同名去重（项目级优先于个人级）、目录不存在容错、非 .md 忽略。
3. **加载正文**：已知技能返回去 frontmatter 的正文；未知名返回清晰 error（不抛）。
4. **prompt 注入**：有技能时系统提示词含"# 可用技能"+ 每条 name/description；无技能时不加多余节；`build_system_prompt(skills=None)` 与原行为一致（回归）。
5. **LoadSkillTool**：走 registry.execute 正常返回；名字路径穿越（`../x`）被拒。
6. **架构**：skills 层不依赖 agent/ui；`_LAYER_RANK` 含 skills。
7. **回归**：现有 179 测试全绿；loop.py 改动后控制流测试不回退。

## 验收标准

1. 放一个 `./.assistant_agent/skills/run-tests/SKILL.md`，启动后系统提示词含其 name/description（Level 1）。
2. 模型调 `load_skill("run-tests")` 返回正文（Level 2）；正文指向的脚本能经现有工具执行（Level 3）。
3. `enabled=false` 时不扫描、不注入、不注册 LoadSkillTool（零副作用）。
4. `/skills` 列出所有已发现技能。
5. 未知技能名、坏 SKILL.md、目录缺失都不使 agent 崩溃。
6. **内核 loop.py 仅加 system_prompt 透传**，run() 控制流未变；现有测试不回退。
7. 新增测试全绿，ruff + 架构测试通过。

## 交付后顺带

- TECH_DEBT 复盘登记新债（若有）。
- 状态文档同步（DoD 第 6 条）：ROADMAP 里程碑表 M7a 标 ✅ + 顶部状态块；CLAUDE/AGENTS/README 当前状态段。数字实测。
