# 基础工具补全方案 — 引入 edit_file（对齐主流 agent）

> 目标：对照 Claude Code / Codex / Copilot 的基础工具集，找共性、补我们缺的。
> 状态：待审阅，未动代码。
> 最后更新：2026-07-02

---

## 一、四家基础工具调研

| 能力 | Claude Code | Codex | Copilot CLI | llama.cpp | 我们 |
|------|:---:|:---:|:---:|:---:|:---:|
| 读文件 | Read | read | file reading | — | ✅ read_file |
| **改文件（局部）** | **Edit** | **apply_patch** | file editing | — | ❌ **缺** |
| 写文件（整篇） | Write | (patch) | file writing | — | ✅ write_file |
| 找文件（名字） | Glob | — | — | — | ⚠️ 仅 list_dir |
| 搜内容 | Grep | grep | search | — | ✅ code_search |
| 执行命令 | Bash | shell | shell execution | — | ✅ run_shell |
| 联网 | WebFetch/Search | — | web fetch/search | — | ⏸ 暂缓 |

**llama.cpp 说明**：它是推理引擎，只提供 function-calling **支持**（模型可调用你定义的工具），
不含内置工具套件——是被上面三家用作后端的底层，不在"基础工具"同一层比较。

**核心发现**：真正的共性基础工具是「**读 / 改 / 写 / 找 / 搜 / 执行**」六件套
（"a coding agent is six functions in a trenchcoat"）。我们唯一缺的关键一件是 **Edit（局部改文件）**。

## 二、深刻理解功能范围（关键）

- **write_file 的范围**：创建/覆盖**整个文件**，**类型无关**——.py/.md/.json/.yaml/任何文本都能写。
  所以"能改很多种文件类型"这一点 write_file **已经覆盖**，不需要为文件类型再加工具。
- **真正的范围缺口不是"类型"，是"粒度"**：write_file 只能"整篇重写"。改一行也要模型
  **重新输出整个文件**——费 token、慢、且**危险**（模型可能漏写/改错其它内容，正是 write_file 的固有风险）。
- 每个成熟 agent 都有 **Edit/apply_patch** 正是为此：**精确替换文件里的一段，不动其余**。
  这才是我们该补的功能范围。

## 三、推荐范围

### 必做：edit_file（局部编辑）
对齐 Claude Code 的 Edit（old_string→new_string + replace_all）。
- 精确替换已存在文件中的一段文本，其余不动。
- 相比 write_file：省 token、更快、更安全（不会误伤未改动内容）。

### 可选（也做）：multi_edit（一次多处编辑）
对齐 Claude MultiEdit：一次给多个 old→new 对，顺序应用、原子写入（任一失败则整体不写）。
应对"一次改多处"的高频需求。

### 关于 apply_diff（为何不做行号 diff）
调研发现各家的"局部改"分两种：
- **字符串替换**（Claude Edit、Roo/Cline 的 apply_diff 底层 SEARCH/REPLACE）——无行号，鲁棒。
- **行号 unified diff**（少数用）——要求模型精确产出行号/上下文，**本地小模型极易数错行、对不齐**，
  违背本项目"对笨模型健壮"的核心原则。
结论：**edit_file/multi_edit 就是 apply_diff 的鲁棒内核**（SEARCH/REPLACE），我们做这个；
不做脆弱的行号 diff。

### 不做
- 行号 unified diff（脆弱，见上）。
- find_files（Glob）：降为以后再说；当前 list_dir + code_search 够用。
- 联网（WebFetch/Search）：安全边界未成熟，暂缓。
- NotebookEdit / TodoWrite / Task：非"基础"或需额外能力，非本期。

## 四、edit_file 设计

```
name: edit_file
description: 精确替换已存在文件中的一段文本（其余不动）。改动局部时优先用它，而非 write_file 整篇重写。
input:
  path: string        # 必填，要编辑的文件（须已存在）
  old_string: string  # 必填，被替换的原文（须在文件中唯一出现，除非 replace_all）
  new_string: string  # 必填，替换成的新文本
  replace_all: bool   # 可选，默认 false；true 则替换所有出现
output:
  成功：ok("已编辑 {path}：替换 N 处")
  失败：error（文件不存在 / old_string 未找到 / 出现多次且未开 replace_all）
```

**行为**：
- 文件不存在 → error（edit 只改已存在文件；新建用 write_file）。
- `old_string` 未找到 → error。
- `old_string` 出现多次且 `replace_all=false` → error（歧义，要求更精确的上下文），
  这是 Claude Code Edit 的关键安全设计：唯一匹配才改，避免误替。
- 编码：读用 UTF-8；沿用 read_file 的容错思路。

**工作区范围**：与 write_file 一致——编辑工作区内文件放行，区外需确认（复用 `_within_workspace` + request_confirm）。

## 五、涉及文件（不动内核）
| 文件 | 改动 |
|------|------|
| `tools/file_ops.py` | 新增 EditFileTool（+ 可选 FindFilesTool） |
| `tools/registry.py` | 注册 |
| `agent/prompts.py` | 提示：局部改用 edit_file，整篇/新建才用 write_file |
| `tests/test_tools.py` | edit 唯一匹配/未找到/多次歧义/replace_all/区外确认 |

内核 `agent/loop.py` **不动**（纯 tools/ 扩展）。

## 六、开发计划（每步带测试）
1. EditFileTool（file_ops）+ 注册 → 单测：唯一替换/未找到 error/多次歧义 error/replace_all/文件不存在/区外确认
2. 提示词：引导优先 edit_file 局部改
3.（可选）FindFilesTool → 单测：按 glob 递归匹配、跳过忽略目录
4. DoD：pytest + ruff + 架构测试全绿；README 工具列表更新

## 七、验收标准
1. edit_file 精确替换唯一匹配；未找到/多次歧义/文件不存在 → 清晰 error
2. replace_all=true 替换所有出现
3. 编辑区外文件走确认
4. 提示词引导"局部改用 edit_file"，实测复杂任务不再整篇重写小改动
5. 新工具带测试；现有测试不回退；ruff + 架构测试通过；内核未动

## 八、风险与边界
- ⚠️ edit_file 的唯一匹配约束是**安全特性**（防误替），不要为图方便默认 replace_all。
- ⚠️ 区外编辑同样要确认（对齐 write_file 的工作区范围）。
- ❌ 不做联网、notebook、todo——非基础或边界未成熟。
- 深刻理解范围：write_file 管"整篇/新建（任意类型）"，edit_file 管"局部精确改"，两者互补不重叠。
