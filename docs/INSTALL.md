# 安装与平台支持

> 如何在各平台安装、运行本项目，以及各平台的验证状态与已知坑。
> 最后更新：2026-07-03

## 前置要求

- **Python 3.11+**（已在 3.14 上验证）
- git（获取源码）
- 目前只有**源码安装**（未发布 PyPI，暂无 `pipx install`）

## 通用安装步骤

```bash
git clone <repo-url> && cd assistant_agent
python -m venv .venv
# 激活虚拟环境（见下方各平台）
pip install -e ".[dev]"     # 开发/测试；只用不开发可去掉 [dev]
```

激活 venv：
- Linux/macOS/WSL2：`source .venv/bin/activate`
- Windows PowerShell：`.venv\Scripts\Activate.ps1`
- Windows CMD：`.venv\Scripts\activate.bat`

运行：
```bash
assistant-agent --help          # 控制台脚本
python -m assistant_agent --help   # 或模块方式
```

## 平台支持矩阵

| 平台 | 状态 | 说明 |
|------|------|------|
| Windows 原生（PowerShell / Windows Terminal） | ✅ 已验证 | 主要开发平台 |
| Windows Git Bash / mintty | ✅ 已验证 | 交互菜单自动降级为编号输入（见下） |
| WSL2（Ubuntu 26.04, Python 3.14） | ✅ 已验证 | 119 测试通过；原生 UTF-8，体验最顺 |
| Linux（原生） | 未验证·高置信 | 依赖有 manylinux wheel；WSL2 已间接佐证 |
| macOS（含 Apple Silicon） | 未验证·高置信 | arm64 wheel 齐全 |
| Termux / Android ARM | 不支持（非目标） | 原生依赖 wheel 可能缺失，需现场编译；本项目不针对此平台 |

> "未验证·高置信"= 依赖与终端能力与已验证平台一致，理论可行但作者未在真机跑过。

## 各平台说明与已知坑

### Windows（原生）
- **别用 Microsoft Store 的 python**：那是占位 stub，会静默失败（exit 49）。用 [python.org](https://www.python.org) 安装，或用 `py` 启动器。
- 已内建处理：终端 UTF-8 输出、GBK 输出解码、系统代理对本地端点的干扰（自动 NO_PROXY）、cmd 交互命令 stdin、Ctrl+C 中断。
- 推荐 Windows Terminal / PowerShell，`/model` 等交互菜单体验最好。

### Windows Git Bash / mintty
- 可正常运行。但 `questionary`（prompt_toolkit）在 mintty 下会报 `NoConsoleScreenBufferError`
  → 已自动**降级为编号输入**（`/model`、`ask_user` 仍可用，只是不是方向键菜单）。功能不缺。

### WSL2 / Linux
- 已验证（WSL2 Ubuntu 26.04）：`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` 一次成功，
  所有原生依赖（tiktoken/tokenizers/pydantic-core 等）直接装 Linux wheel，无需编译。
- 原生 UTF-8，中文与表格渲染完整，交互菜单完整支持。
- **注意**：若项目源码在 Windows 挂载盘（`/mnt/...`），**不要复用 Windows 侧的 `.venv`**
  （里面是 Windows 的 python.exe）。在 Linux 侧单独建 venv。

### macOS
- 未在真机验证，但依赖 arm64/x86_64 wheel 齐全、终端原生 UTF-8，预期与 Linux 一致。

## 配置（安装后）

```bash
cp config.example.yaml config.yaml
```
- `config.yaml` 已 gitignore，不会提交。
- **API key 用环境变量**：在 YAML 里写 `api_key: ${OPENAI_API_KEY}`，加载时自动从环境变量取值——
  配置文件里存的是变量名，不是明文 key。
- 本地后端（LM Studio/Ollama/vLLM）：填 `api_base`（如 `http://localhost:1234/v1`）+ 占位 key。
- 切换后端：改 `config.yaml` 顶部 `active`，或运行时用 `--provider` / 对话内 `/model`。

## 验证安装

```bash
pytest                                  # 全测试应通过
assistant-agent providers -c config.example.yaml   # 列出示例 provider
assistant-agent run "列出当前目录有哪些文件"        # 需已配好可用后端
```

## 未来（暂未做）

- 发布 PyPI / 支持 `pipx install`
- `assistant-agent init` 交互式配置向导（见 docs/archive/phase1/m5-init-plan.md）
- macOS / 原生 Linux 真机验证
