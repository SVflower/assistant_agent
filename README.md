# Assistant Agent

一个跑在本地、**模型后端可自由切换**（云端 API / 本地 LM Studio / vLLM）的通用任务 Agent，编码能力优先。

核心卖点：换模型只改 `config.yaml`，业务代码零改动。

## 安装

```bash
python -m venv .venv          # Python 3.11+
# 激活 venv 后：
pip install -e ".[dev]"
```

各平台安装步骤、平台支持矩阵与已知坑见 [docs/INSTALL.md](docs/INSTALL.md)（Windows / WSL2 已验证）。

## 配置

复制模板并填写：

```bash
cp config.example.yaml config.yaml
```

`config.yaml` 已被 gitignore，不会提交。API key 建议用环境变量（在 YAML 里写 `${ANTHROPIC_API_KEY}`），或直接填入。

切换后端：改 `config.yaml` 顶部的 `active` 字段指向某个 provider 即可。

## 使用

```bash
# 单次任务
assistant-agent run "读取 README.md，在末尾追加一行 changelog，然后列出当前目录确认"

# 交互模式（默认新建会话，自动保存）
assistant-agent chat

# 恢复历史会话续接
assistant-agent chat --resume <会话id>

# 列出 / 删除历史会话
assistant-agent sessions
assistant-agent sessions --delete <会话id>

# 模型/后端管理
assistant-agent providers                 # 列出所有 provider
assistant-agent run "..." --provider local_lmstudio   # 临时指定后端（-p，覆盖 active）
# 对话中输入 / 或 /help 查看所有命令（/model 切模型、/clear 新会话、/context 看用量、/sessions、/exit）

# 轮数上限（复杂任务不够时提高）
assistant-agent run "..." --max-iterations 30

# 指定配置文件
assistant-agent run "..." --config /path/to/config.yaml
```

会话存于项目下 `./.assistant_agent/sessions/`（已 gitignore）。

## 开发

```bash
pytest                          # 跑测试
ruff format . && ruff check .   # 格式化 + lint
```

## 架构

```
config/   配置加载与校验（Pydantic + YAML）
cli/      slash 命令系统（/help /model /clear /context 等，本地控制、不进循环）
llm/      模型抽象层（封装 LiteLLM，统一云端/本地）
tools/    工具系统（base/registry + 内置：读/写/局部编辑/多处编辑/列目录/shell/代码检索/git 只读/澄清）
session/  会话持久化（JSON 存档，跨会话续接）
agent/    ReAct 主循环 + 上下文管理 + 提示词
ui/       终端输入输出（Rich）
```

扩展点：换模型动 `config.yaml`；加能力在 `tools/` 加文件并在 `registry.py` 注册——内核 `agent/loop.py` 不动。

详见 [DESIGN.md](DESIGN.md)。
