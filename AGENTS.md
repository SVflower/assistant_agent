# AGENTS.md

> 始终使用中文与我交互。
> 给 Codex 的项目说明。保持精简——这个文件每轮都进上下文。
> 详细设计见 [DESIGN.md](DESIGN.md)。

## 项目是什么

一个跑在本地、**模型后端可自由切换**（云端 API key / 本地 LM Studio / vLLM）的通用任务 Agent，编码能力优先。
核心卖点：换模型只改 `config.yaml`，业务代码零改动。

## 技术栈

- Python 3.11+
- LiteLLM（模型统一层，所有 provider 走 OpenAI 兼容格式）
- Pydantic + YAML（配置）
- Typer（CLI）
- pytest（测试）

## 命令

```bash
# 安装依赖（开发模式）
pip install -e ".[dev]"

# 跑测试
pytest

# 跑单个测试文件
pytest tests/test_tools.py

# 覆盖率（让未测盲区显形）
pytest --cov

# 格式化 + lint
ruff format . && ruff check --fix .

# 启动 agent
python -m assistant_agent
```

## 铁律（必须遵守）

1. **绝不在业务逻辑里写死 provider。** 所有模型调用走 `llm/client.py` 的抽象层。换后端是改配置的事，不是改代码的事。
2. **绝不提交密钥。** API key 只进 `config.yaml`（已 gitignore）或环境变量。`config.example.yaml` 永远不含真实 key。
3. **改完代码必须跑 `pytest` 和 `ruff`**，确认通过再说完成。
4. **内核职责稳定、实现受控演进**：`agent/loop.py` 是内核。加能力**优先** = 在 `tools/` 加文件并注册；确需改循环（预算/终止/恢复等内核职责）时允许，但**改动前必先向用户确认、改后现有测试不回退**。
5. **新功能要带测试。** 工具、配置、循环的改动都要有对应测试。

## 约定

- 工具实现放 `tools/`，继承 `base.py` 的基类，在 `registry.py` 注册。
- shell 工具：删除/覆盖/移动等危险操作前必须向用户确认；普通命令直接执行。
- 上下文管理要做长度感知截断——本地模型上下文窗口比云端小得多。
- 错误处理要对"笨模型"健壮：本地小模型的工具调用格式经常不规范，解析要容错、要重试。

## 里程碑工作流（较大任务默认遵守）

做较大任务（新里程碑、新特性、动多文件）时，默认走这套流程，不用用户每次提醒；
小改动（改 bug、单文件微调）可直接做。

1. **调研**：参考成熟产品/公开资料，总结可借鉴原则（不照搬），拿不准的标注"不确定"，不编造。
2. **评估**：读现有代码，判断当前架构是否适合扩展、是否需要动内核。
3. **方案**：落成 `docs/<里程碑>-plan.md`，含范围（必做/可选/不做）、技术设计、是否动内核、测试计划、验收标准、风险边界。
4. **审阅**：先出方案，用户确认后再写代码；**动内核 `agent/loop.py` 前必须先问用户**。
5. **实现**：分步进行，每步带测试。
6. **验收**：按方案的验收标准 + 下面的 DoD 全绿，才算完成。

## 质量护栏（防迭代劣化）

- **架构适应度测试** `tests/test_architecture.py`：自动检查分层依赖（config→llm→tools→agent→ui→main，只能依赖同层或更低层）、内核 UI 无关、工具不反向依赖、单文件行数（软线 300 仅警告交人评审、硬线 500 才失败）。**报红时应拆分/修依赖，而不是放宽规则。**
- **技术债登记册** `docs/TECH_DEBT.md`：新债即时登记，每次里程碑评审更新，防隐形复利。
- **覆盖率** `pytest --cov`：不设强制门槛，但关键路径（流式碎片拼接、confirm 解析）低覆盖要显形并补测。

## 里程碑完成定义（DoD）

每个里程碑退出前必须全部满足：
1. `pytest` 全绿（含架构测试），`ruff check` 全绿。
2. 本里程碑新增/改动的**关键路径有测试**（不追全覆盖，但别留脆弱逻辑裸奔）。
3. 发现的新技术债已登记进 `docs/TECH_DEBT.md`。
4. 无密钥/垃圾文件入库（提交前审查 `git diff --cached`）。
5. 动了内核 `agent/loop.py` 时，说明理由并确认现有测试不回退。
6. **状态文档同步**：更新 ROADMAP 里程碑表的状态标记 + 顶部"项目当前状态"块，以及本文件、`CLAUDE.md`、`README.md` 的"当前状态"段。数字（测试数/覆盖率/源码行数/剩余技术债）用**实测**（`pytest -q`、`--cov`、`wc -l`），不凭记忆。里程碑历史小节里的旧数字是当时快照，不回改。

## 当前状态

**第一阶段（MVP→M5）全部完成**，第二阶段进行中。里程碑详情见 ROADMAP.md。
- 第一阶段：配置/模型抽象/工具/ReAct 循环/CLI，加流式输出、会话持久化、工具集扩展（edit/multi_edit/code_search/git 只读）、模型切换、循环工程与写入安全、slash 命令、init 向导，全部落地。
- **第二阶段 M6/M6.5/M7a/M7b 已完成**：结构化日志与工具审计；任务级工具调用/累计输出预算与批次协议完整终止；Agent Skills 系统（SKILL.md 发现 + 渐进披露 + load_skill）；MCP client stdio（外部 server 工具接入 + 同步桥 + 命名空间 + 每工具确认 + 过滤/上限 + cli/setup.py Runtime，还清 D7）。
- 双后端实测通过：云端 DeepSeek + 本地 LM Studio，切换只改 `config.yaml`，业务代码零改动。
- 194 个测试通过，ruff 全绿。

下一步（评审后顺序）：~~M7b（MCP stdio）已完成~~ → M8a（上下文预算口径，还 D10）→ M8b（摘要压缩）→ M7c（HTTP，待稳定）。计划见 docs/m8a-context-budget-plan.md、m8b-context-compaction-plan.md、m7c-mcp-http-plan.md 与 ROADMAP.md。
