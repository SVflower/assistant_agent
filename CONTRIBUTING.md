# Contributing

感谢参与 Assistant Agent。请先阅读 [README.md](README.md)、
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 和 [SECURITY.md](SECURITY.md)。

## 开发环境

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
pytest -q
ruff format --check .
ruff check .
python -m mypy src/assistant_agent
```

真实模型、外部 MCP、个人 Skills、配置文件和运行产物不应进入 Pull Request。

## 提交要求

- 新行为必须有针对性测试。
- 不提交 API key、Session/Run、日志、截图、构建目录或本地配置。
- 公共 contract、Run/Session 状态和错误码变更必须同步正式契约文档。
- 不让 API/Web 复制 Agent 的状态机；先确认所有权和依赖方向。
- 涉及 Loop、权限、沙箱或恢复语义时，说明风险和回滚方式。

## Pull Request

请说明问题、方案、测试命令和未覆盖的环境。一个 PR 聚焦一个主题，
不要混入个人重构、全仓格式化或生成文件。
