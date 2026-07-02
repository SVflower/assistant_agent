# CLAUDE.md

> 给 Claude 的项目说明。保持精简——这个文件每轮都进上下文。
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
4. **内核保持封闭**：`agent/loop.py` 是稳定内核。加能力 = 在 `tools/` 加文件并注册，不改循环。
5. **新功能要带测试。** 工具、配置、循环的改动都要有对应测试。

## 约定

- 工具实现放 `tools/`，继承 `base.py` 的基类，在 `registry.py` 注册。
- shell 工具：删除/覆盖/移动等危险操作前必须向用户确认；普通命令直接执行。
- 上下文管理要做长度感知截断——本地模型上下文窗口比云端小得多。
- 错误处理要对"笨模型"健壮：本地小模型的工具调用格式经常不规范，解析要容错、要重试。

## 质量护栏（防迭代劣化）

- **架构适应度测试** `tests/test_architecture.py`：自动检查分层依赖（config→llm→tools→agent→ui→main，只能依赖同层或更低层）、内核 UI 无关、工具不反向依赖、单文件 ≤300 行。**报红时应拆分/修依赖，而不是放宽规则。**
- **技术债登记册** `docs/TECH_DEBT.md`：新债即时登记，每次里程碑评审更新，防隐形复利。
- **覆盖率** `pytest --cov`：不设强制门槛，但关键路径（流式碎片拼接、confirm 解析）低覆盖要显形并补测。

## 里程碑完成定义（DoD）

每个里程碑退出前必须全部满足：
1. `pytest` 全绿（含架构测试），`ruff check` 全绿。
2. 本里程碑新增/改动的**关键路径有测试**（不追全覆盖，但别留脆弱逻辑裸奔）。
3. 发现的新技术债已登记进 `docs/TECH_DEBT.md`。
4. 无密钥/垃圾文件入库（提交前审查 `git diff --cached`）。
5. 动了内核 `agent/loop.py` 时，说明理由并确认现有测试不回退。

## 当前状态

第一阶段（MVP）已完成并验证：
- 配置系统、模型抽象层、工具系统、ReAct 循环、CLI 全部跑通。
- 双后端实测通过：云端 DeepSeek + 本地 LM Studio，切换只改 `config.yaml`，业务代码零改动。
- shell 工具 bug 已修：交互命令 stdin 切断（不再卡超时）、提示词注入 OS/日期、输出编码容错。
- 28 个测试通过，ruff 全绿。

下一阶段：流式输出（让思考/等待过程透明），会动 `agent/loop.py` 内核，见 DESIGN.md 第 9 节。
