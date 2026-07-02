# M3 实施方案 — 记忆与会话持久化

> 目标：解决"跨会话零记忆，重启忘光"。
> 状态：待审阅，未动代码。对应 [ROADMAP.md](../ROADMAP.md) M3。
> 最后更新：2026-07-02

---

## 一、记忆分层与本期范围（含技术背书）

业界主流框架（[LangChain](https://www.langchain.com/blog/how-to-give-your-agent-memory)、
[Augment Code](https://www.augmentcode.com/guides/agent-memory-vs-context-engineering)、
[Azure Databricks](https://learn.microsoft.com/en-us/azure/databricks/generative-ai/agent-framework/stateful-agents)）
的核心区分不是"短/中/长"，而是两个更本质的概念：

- **Agent Memory（记忆）**：跨会话存活的**外部存储**（文件/数据库/向量库）。
- **Context Engineering（上下文工程）**：决定从存储里**选什么加载进模型窗口**。

据此摆正 M3 的两件事（避免把两者混为一谈）：

| 事项 | 准确定位 | 本期 |
|------|---------|------|
| 上下文窗口内"选什么/丢什么" | **上下文工程**（短期，窗口内） | ✅ 本期夯实：token 感知截断 |
| 对话存盘、跨会话续接 | **Agent Memory 持久化**（中期） | ✅ **核心工作** |
| 跨会话语义召回（向量库） | Agent Memory 最重的一层 | ❌ 明确不做——业界公认最复杂，克制是合理取舍 |

> 说明：之前"短/中/长"的叫法只是直觉分层；这里用业界准确框架校正，
> 不影响本期实际范围（上下文工程 + 持久化）。

## 二、已确认决策

| 项 | 决策 |
|----|------|
| 存储格式 | JSON 文件（每会话一个文件，简单透明无依赖） |
| 存储位置 | 项目目录下 `./.assistant_agent/sessions/`（会话跟项目走） |
| 恢复方式 | 启动默认新会话；提供命令列出/恢复/删除历史会话 |

## 三、短期（上下文工程）：token 感知截断

**现状**：`Conversation._truncated` 按"消息数量"（`max_history_messages`）保留最近 N 条。
问题：消息长短不一，条数控制不住真实 token 量，本地小模型上下文窗口小，仍可能超窗。

**为什么必须按 token 而非消息数（技术背书）**：
- 本地小模型的硬上限是 **token**，且**服务端不兜底**——超了直接报错。
  实例：[PolarGrid](https://polargrid.mintlify.app/guides/context-management) 的 `qwen-3.5-27b`
  硬上限 **8192 token**，涵盖 system + 历史 + 回复。按消息条数根本控不住这个约束。

**改法**：
- 新增按 **token 预算**截断：保留 system + 尽量多的最近消息，使总 token ≤ `max_context_tokens`。
- token 计数用 `litellm.token_counter(model, messages)`；本地模型无对应编码时回退近似
  （字符数/常数）——只需"足够安全"，不求精确。
- 保留现有保护：不切断 assistant↔tool 配对（截断后丢弃开头孤立的 tool 消息）。
- 配置：`agent.max_context_tokens`（新增，默认如 8000）；`max_history_messages` 保留为硬上限兜底。

**保留"头 + 尾"是有意为之（应对 lost-in-the-middle）**：
- 研究与生产实践一致发现 [lost-in-the-middle](https://www.learnwithparam.com/blog/context-window-management-production-ai-agents)：
  模型对上下文**开头和结尾注意力强、中间读而不见**，且
  [性能在触达上限前就下降](https://tianpan.co/blog/2025-10-20-token-budget-strategies-llm-production)。
- 我们的策略（永远保留 system=头、保留最近消息=尾）正好把关键信息放在模型最"看得见"的位置，
  这是**依据注意力规律的设计**，不是随手截。

**为什么选截断而非摘要（诚实标注缺陷与升级路径）**：
参考 [长时程 agent 记忆分析](https://notes.muthu.co/2026/03/working-memory-compression-and-context-distillation-in-long-horizon-agents/)：

| 策略 | 优点 | 缺点 | 本期 |
|------|------|------|------|
| 滑动窗口截断（留最近） | 简单可靠、零额外调用 | **武断切除历史**，agent 可能重试已做过的步骤 | ✅ 采用 |
| 周期性摘要压缩 | 保留更久信息 | 复杂、需额外 LLM 调用、可能丢细节 | ⏸ **明确的后续升级路径** |

M3 用截断是正确起步；其已知缺陷（切史、可能重试旧步骤）记录在案，后续可升级为摘要压缩。

## 四、中期记忆：会话持久化

### 会话文件格式（JSON）
```
./.assistant_agent/sessions/<session_id>.json
{
  "id": "20260702-153000-a1b2",
  "created_at": "...", "updated_at": "...",
  "provider": "cloud", "model": "openai/deepseek-v4-pro",
  "messages": [ {role, content, ...}, ... ]   // 不含 system（运行时重建）
}
```
- `session_id`：时间戳 + 短随机后缀，可读、可排序、不撞。
- 不存 system 消息（含日期/环境，运行时按当前重建）。

### 新增模块 `session/store.py`
- `SessionStore(dir)`：`save(session)` / `load(id)` / `list()` / `delete(id)`。
- `Conversation` 增加 `to_messages()` / `load_messages(list)`，供存取历史（system 不入档）。

### 保存时机
- `chat` 每完成一轮 `run()` 后自动保存当前会话（增量覆盖写整个文件，简单可靠）。
- `run` 单次模式：默认**不**持久化（一次性任务，无跨会话意义）；如需要可后续加 `--save`。

### CLI 命令
- `chat`：默认**新建**会话，自动保存。
- `chat --resume <id>`：加载指定会话，续接对话。
- `sessions`：列出历史会话（id、时间、首条消息预览、消息数）。
- `sessions --delete <id>`：删除指定会话（删除是不可逆操作 → 复用确认机制，先问）。

## 五、涉及文件

| 文件 | 改动 | 动内核？ |
|------|------|:---:|
| `agent/context.py` | token 感知截断 + to/load messages | 否 |
| `session/store.py` | 新增：会话存取 | 否（新模块） |
| `config/schema.py` | 新增 `max_context_tokens` | 否 |
| `main.py` | chat 自动保存、`--resume`、`sessions` 命令 | 否 |
| `.gitignore` | 忽略 `.assistant_agent/` | — |
| `tests/` | 新增 store / 截断 / 恢复测试 | — |

内核 `agent/loop.py` **不动**。

## 六、验收标准（对齐 ROADMAP M3）
1. `chat` 退出重启后，`sessions` 能**列出**历史会话
2. `chat --resume <id>` 能**恢复**并续接（模型记得之前内容）
3. `sessions --delete <id>` 能**删除**（带确认）
4. 上下文截断改为 **token 感知**，构造超长历史不超预算
5. 新增测试全绿，现有 42 测试不回退，ruff 通过

## 七、不在本期范围（及明确的升级路径）
- **长期记忆 / 向量检索**：Agent Memory 最重的一层，本期不做。
- **摘要压缩**：截断的已知升级路径（见第三节）；历史增长到截断明显丢信息时再上。
- `run` 模式持久化（默认一次性）。
- 会话搜索、重命名、导出等管理增强（后续按需）。

## 八、实施步骤（每步可验证）
1. token 感知截断（`context.py` + config）→ 单测：构造超长历史，验证按 token 截断且不破坏配对
2. `SessionStore`（新模块）→ 单测：save/load/list/delete 往返
3. `Conversation` to/load messages → 单测：往返一致（system 不入档）
4. CLI 接线：chat 自动保存 + `--resume` + `sessions` 命令 → 手动验证列出/恢复/删除
5. `.gitignore` 忽略 `.assistant_agent/`；跑 pytest + ruff 全绿

## 九、技术背书来源
- [LangChain: How to Build Memory into AI Agents](https://www.langchain.com/blog/how-to-give-your-agent-memory) — 短期/长期记忆定义
- [Agent Memory vs. Context Engineering (Augment Code)](https://www.augmentcode.com/guides/agent-memory-vs-context-engineering) — 记忆 vs 上下文工程的本质区分
- [Context window management in production](https://www.learnwithparam.com/blog/context-window-management-production-ai-agents) — lost-in-the-middle
- [Token Budget Strategies (tianpan.co)](https://tianpan.co/blog/2025-10-20-token-budget-strategies-llm-production) — 触限前性能下降
- [Working Memory Compression in Long-Horizon Agents](https://notes.muthu.co/2026/03/working-memory-compression-and-context-distillation-in-long-horizon-agents/) — 截断 vs 摘要权衡
- [Managing Context in Long Conversations (PolarGrid)](https://polargrid.mintlify.app/guides/context-management) — 本地模型 8192 token 硬上限、服务端不兜底
