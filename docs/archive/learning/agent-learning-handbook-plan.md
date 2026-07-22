# Agent 项目学习手册实施记录

## 目标

为 Python 基础尚不熟练的读者提供一套基于当前真实代码的学习材料，说明项目分层、启动装配、
Agent Loop、工具安全链、Session/Run 恢复和外部集成，并在关键源码中补充解释设计原因的注释。

## 范围

- 新增 `docs/learning/` 分章节手册和阅读练习。
- 在 README 增加学习入口。
- 为 composition root、Application Service、状态模型、工具 Registry、持久化和 adapter 增加教学注释。
- 不改变任何运行逻辑，不增加兼容层，不修改 `agent/loop.py`。

## 注释原则

1. 解释职责、调用顺序、失败边界和状态不变量。
2. 对 `yield`、Protocol、Pydantic、context manager 等项目中实际出现的 Python 写法给出上下文。
3. 不逐行翻译显而易见的赋值和条件判断。
4. 注释不能成为第二份规范；稳定契约仍以 `docs/agent-service-integration-guide.md` 为准。

## 验收

- 初学者能沿 CLI 和 Service 两条调用链找到关键文件。
- 能区分 Conversation、Session 与 RunState。
- 能解释一次工具调用经过的校验、权限、预算、checkpoint、执行和审计顺序。
- Ruff、mypy、import-linter 和 pytest 不回退。
- 公共服务契约无变化：本任务只增加文档和注释，不修改导出、DTO、事件、状态或生命周期语义。

## 完成结果（2026-07-21）

- 新增 7 章学习手册和总入口，README 已建立链接。
- 15 个关键源码文件增加职责、不变量和失败边界注释，未修改 `agent/loop.py` 或运行语句。
- 超过 600 行的既有模块仅因注释增长，实测行数和复审结论已同步到架构事实源；未新增技术债。
- `ruff format --check .`、`ruff check .`、mypy（132 files）、import-linter（12/12）全绿。
- 全量 `pytest -q`：797 passed、10 skipped；`pytest --cov` 实测覆盖率 84%。coverage 首轮仅出现一次
  Windows 强杀子进程后的 lease 瞬时释放波动，单测复跑与随后全量均通过。
- 公共服务契约无变化，无 API/Web 必改项，事件契约与 checkpoint schema 版本不变。
