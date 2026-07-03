# M4.7 实施方案 — 循环工程与写入安全

> 起因：真实使用中发现两个问题——① agent 在复杂循环里**自作主张改文件**（write_file 零确认）；
> ② 最大轮数 15，用尽即硬失败。这是 loop-engineering 该补的控制/安全/终止缺口。
> 状态：待审阅，未动代码。用户已批准**动内核**。
> 最后更新：2026-07-02

---

## 一、结论

补两块：**写入安全**（治"自己动文件"）+ **循环控制**（治轮数/卡死）。
适配四家的成熟做法，但按单人本地 CLI 的规模裁剪，不引入重框架。

## 二、参考借鉴（四家，含取舍）

| 来源 | 可借鉴 | 适配到本项目 |
|------|--------|-------------|
| Claude Code | acceptEdits **自动放行编辑**、"don't ask again"记忆、allow 规则；permission modes 分档 | 采纳"区内写自由、确认按范围一次性给"；权限模式**轻量 3 档**（可选） |
| Codex | **workspace-write**：区内写**自动放行**、区外/网络才升级授权（沙箱担保区内安全） | 采纳"工作区范围"：区内自由、**只拦区外写**；无沙箱则靠可见+中断+git 兜底 |
| loop-engineering 通则 | 终止/卡死检测/成本可见 | 采纳重复动作熔断 + 用尽轮数优雅处理 |
| Hermes/openclaw | 路由/fallback/OS 沙箱 | ❌ 不采纳：过重，非本阶段 |

**明确不搬**：OS 级沙箱原语（太重）、bypass 全放模式（危险）、网络沙箱（尚无网络工具）。

## 三、问题根因（基于代码）
- **问题A**：`tools/file_ops.py` `WriteFileTool.run` 直接 `mkdir + write_text`，**无任何范围限制**——能写任意路径（含项目目录外），用户毫不知情。M2.5 两层确认只覆盖 shell 危险命令，没有"工作区边界"概念。
- **问题B**：`agent/loop.py` 用尽 `max_iterations` 就 `yield error`，生硬终止；且无重复动作检测，模型卡在循环里会空耗到上限。

## 四、设计

### Part 1：写入安全（工具层，不动内核）

**原则（对齐 Codex workspace-write / Claude acceptEdits）：确认按"范围"一次性给，不逐个文件问。**
划定安全区（工作区=启动 cwd），区内自由、区外才拦：

| 写操作 | 处理 | 理由 |
|--------|------|------|
| **工作区内**（项目目录下，含覆盖/新建） | **自动放行，不问** | 正常写代码；靠"流式可见 + Ctrl+C 中断 + git 回滚"兜底 |
| **工作区外**（项目目录之外） | `request_confirm("write_outside_workspace", ...)` | 这才是"自己动别处文件"的真正意外，值得拦 |

**为何区内不逐个问**：四家 agent 的共识是——安全网靠**可见 + 可中断 + 可回滚**，不靠弹窗轰炸：
- 可见：每个写操作已在流里显示（`→ 调用工具 write_file(...)`，本项目已有）
- 可中断：Ctrl+C（M2.5 已有）
- 可回滚：git diff/revert（M4 已有 git 只读）
逐个确认会烦死人，且四家都不这么做（Codex 区内沙箱自由、Claude acceptEdits 自动放行）。

**判定**：`path.resolve()` 是否在 `Path.cwd().resolve()` 子树内。区外 → 确认（复用 always_allowed 记忆）。

**可选更严模式**（见 Part 2）：`strict` 档下区内写也"首次问一次 + 永久允许"，给谨慎用户。

### Part 2：权限模式（可选，轻量 3 档）
适配 Claude Code 的 modes，但只留 3 档，config/`--mode` 设：
- `readonly`：禁所有写与 shell 执行（只读/检索/git-read）——安全探索/复杂任务先看不动手。
- `default`（默认）：**区内写自由、区外写确认**（Part 1）；shell 危险命令确认（现状）。
- `strict`：更严——区内写也"首次问一次 + 永久允许"，给谨慎用户。
> 标记为**可选**：核心价值在 Part 1 的区外拦截；模式是锦上添花，可后置。
> 注意 `readonly` 要在工具层真的禁写，不能只靠提示词。

### Part 3：循环控制（动内核 loop.py，已批准）
**3a 重复动作熔断**：循环内记录最近若干次 `(tool_name, 规范化 args)` 签名；
同一签名**连续重复 ≥ 阈值（默认 3）** → 判定卡死，`yield error("检测到重复动作，已停止避免死循环")` 并终止。
**3b 用尽轮数优雅处理**：注入 `continue_check: Callable[[int], bool] | None`（复用 interrupt_check 的注入模式）：
- 交互(chat)：用尽时 main 问"已到 N 轮，继续吗？"，是则再放一批、否则带总结停。
- 单次(run)：不注入 → 用尽即停，但输出"已达上限，做了哪些、还差什么"的说明而非干巴巴报错。
**3c 轮数可配**：`--max-iterations` 启动覆盖 config。

### 内核改动范围（loop.py）
- `run()` 循环体内加：签名记录 + 重复检测分支；用尽轮数分支改为"问 continue_check / 带总结"。
- `AgentLoop.__init__` 加 `continue_check` 参数（默认 None，行为不变）。
- **不改**事件模型主体、工具执行、历史写回逻辑。改完现有循环测试须全绿。

## 五、权限
- **区外写**确认复用 `request_confirm("write_outside_workspace", ...)`；`always_allowed` 记忆。
- 区内写：直接放行（default 模式）。
- 循环控制无副作用、不涉确认。

## 六、错误处理
- 区外写被拒 → `ToolResult.error("用户拒绝写入工作区外：X")`，模型换做法。
- 重复熔断/用尽轮数 → 明确事件文本，不崩、不假装成功。

## 七、涉及文件
| 文件 | 改动 | 动内核？ |
|------|------|:---:|
| `tools/file_ops.py` | write 确认 + 工作区范围判断 | 否 |
| `agent/loop.py` | 重复熔断 + 用尽轮数优雅 + continue_check | **是（已批准）** |
| `tools/base.py` | （若做模式）ToolContext 加 permission_mode | 否 |
| `config/schema.py` | （可选）permission_mode、max_iterations 已有 | 否 |
| `main.py` | 注入 continue_check（chat 问用户）；`--max-iterations`；（可选）`--mode` | 否 |
| `agent/prompts.py` | 补"勿改无关文件、改前说明范围" | 否 |
| tests | write 确认、重复熔断、用尽续问、工作区范围 | — |

## 八、开发计划（每步带测试）
1. 工作区范围写入（file_ops）→ 单测：区内写放行/区外写确认/拒绝不写/永久允许
2. 提示词约束（勿动无关文件）
3. 重复动作熔断（loop）→ 单测：同签名重复达阈值即熔断（FakeStreamClient 构造）
4. 用尽轮数优雅 + continue_check（loop + main）→ 单测：注入 continue_check 返回 True 续、False 停带总结
5. `--max-iterations`；（可选）权限模式 3 档
6. DoD：pytest + ruff + 架构测试全绿；现有循环测试不回退

## 九、验收标准
1. write_file 写**工作区内**（含覆盖/新建）→ 直接放行不问；写**工作区外** → 弹确认；拒绝则不写
2. "永久允许"后区外写不再问
3. 模型连续重复同一动作达阈值 → 熔断终止，提示清晰（不再空耗到上限）
4. 用尽轮数：chat 问"继续吗"、run 带总结停（不再干巴巴报错）
5. `--max-iterations` 生效
6. 新增测试全绿；现有循环测试不回退；ruff + 架构测试通过；内核控制流改动有测试覆盖

## 十、风险与边界
- ⚠️ 动内核只在 run() 加"重复检测 + 用尽分支"，不重构事件/工具/历史逻辑；改完确认现有循环测试全绿。
- ⚠️ 写确认别太烦：**区内一律放行**（含覆盖/新建），只拦区外；靠可见+中断+git 兜底、always_allowed 记忆。
- ❌ 不做 OS 级沙箱、bypass 全放模式、网络沙箱——过重/危险/无对应工具。
- ⚠️ 权限模式若做，`readonly` 要真的禁写（工具层校验），不能只靠提示词。
