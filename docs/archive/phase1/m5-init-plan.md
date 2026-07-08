# M5 实施方案 — 上手体验（assistant-agent init）

> 目标：新用户运行 `init` 走交互向导，完成选后端/配 key/检测环境/生成 config.yaml，之后能直接 run。
> 状态：待审阅，未动代码。
> 最后更新：2026-07-03

---

## 1. 结论：适合现在做

适合。前置能力都就位：config schema、`${VAR}` 环境变量展开、provider 列表、endpoint 概念、CLI 框架、cli/ 命令层。
init 是**纯新增命令 + 配置生成**，不动内核、不改 schema（见安全设计），兼容风险低。

## 2. 现状审计（基于代码）

**配置结构**（`config/schema.py`）：
```
AppConfig{ active, providers: {名字→ProviderConfig{model, api_key?, api_base?, temperature, max_tokens}},
           agent, tools, ui }
```
**加载逻辑**（`config/loader.py`）：
- 从 cwd 向上找 `config.yaml`；YAML 解析 → **递归展开 `${VAR}` / `${VAR:-default}`** → Pydantic 校验。
- **关键**：`api_key: ${OPENAI_API_KEY}` 这种写法**已被支持**——加载时从环境变量取值。这是 M5 安全设计的基石。
**CLI 入口**：`main.py`（typer）已有 run/chat/sessions/providers；命令层在 `cli/`。
**provider/client 初始化**：`main._build_client` → `LLMClient(config.active_provider)`；`llm/client.py` 有本地端点 NO_PROXY 豁免。
**run 依赖的配置**：`active` + 对应 provider 的 `model`（必填）、`api_key`/`api_base`（可选）。
**已有环境变量读取**：loader 的 `${VAR}` 展开；`llm/client.py` 的 NO_PROXY 处理。
**测试结构**：test_config（加载/env 展开/切换）、test_commands（cli）；**无 CLI 端到端交互测试**（init 需补）。

**当前新用户要手动做的**：① 复制 config.example.yaml→config.yaml ② 改 active ③ 填 model
④ 填 api_key 或设环境变量 ⑤ 本地还要填 api_base ⑥ 自己确认端点通不通。**门槛高、易错、易明文写 key。**

**M5 新增**：`init` 命令 + `cli/init.py`（向导/生成/env 检测/endpoint 检测/校验）。
**是否调整现有加载逻辑**：不需要——`${VAR}` 展开已够用。
**兼容风险**：低——只新增命令与文件；不改 schema、不改 loader、不动 run/chat。

## 3. 需求调研（成熟 CLI onboarding 原则，择善而用）

参考 `gh auth login`、Vercel/Railway/Fly CLI、Claude Code/Codex 的配置思路，提炼**适合本项目**的原则：
- **交互式为默认**：新用户不知道有哪些字段/格式，向导逐步问答比让人手写 YAML 门槛低得多（可发现性，同 slash 的理念）。
- **密钥走环境变量、不进配置文件**：`gh`/Vercel 都把凭据存在专门位置，不塞进项目可读文件。我们对应做法：config 只写 `${VAR}`，真实 key 留在环境变量。
- **敏感输入不回显**：`gh` 输 token 用无回显。
- **检测即时反馈**：配完本地端点马上测连通，早暴露问题。
- **非交互参数**：CI/高级用户需要 `--provider ... --yes`。**本项目 M5 列为可选**（先把交互跑通）。
- **已存在配置要保护**：备份 + 确认，不静默覆盖。

**写 config.yaml 的 / 放环境变量的**：
| 写进 config.yaml | 放环境变量 |
|---|---|
| provider 名、model、api_base（本地端点非机密）、`api_key: ${VAR}`（只是变量名）、参数 | **真实 API key** |
| 本地占位 key（如 lm-studio/EMPTY，非机密）| |

## 4. 安全策略（重点）

**核心决策：不新增 `api_key_env` 字段，复用现有 `${VAR}` 展开。**
- config 里写 `api_key: ${OPENAI_API_KEY}`——**存的是变量名，不是 key**；加载时从环境变量取。
- 好处：**零 schema 改动、零兼容风险**，且天然满足"配置不含明文 key"。优于新增字段（重复且要改 schema）。

**init 默认不接触真实 key（最安全）**：
- 云端 provider：向导问"用哪个环境变量名"（默认按 provider 猜，如 openai→`OPENAI_API_KEY`），
  **检测该变量是否已设**：已设 → ✅；未设 → 打印如何设置的指引（不读取、不写入 key）。
- config 里只写 `api_key: ${那个变量名}`。**init 全程不读、不存真实 key。**
- 若用户坚持当场输入 key：用无回显输入（getpass），且**只提示如何 export 成环境变量，绝不写进 config**；
  当前终端无法隐藏输入时，明确提示并拒绝接收。
- 本地端点：写占位 key（lm-studio/EMPTY，非机密）+ api_base。
- 日志/错误/事件流：不输出 key（本就不持有）。
- 已存在 config.yaml：备份为 `config.yaml.bak`（带时间戳）后再写，或取消；不静默覆盖。
- 端点检测：带 timeout（如 3s）；本地地址走 NO_PROXY 豁免（复用现有逻辑）。
- **不自动写 shell profile / 不注册系统环境变量**（本期暂缓，只打印指引让用户自己做）。

## 5. 技术实现方案

**新增命令**：`assistant-agent init`（typer 命令，在 main.py 注册，逻辑在 cli/init.py）。

**新增模块 `cli/init.py`**（分职责的小函数，便于测试）：
- `run_init(console, config_path, assume_yes=False)`：编排向导。
- `_choose_provider_kind()`：列**当前实际支持**的后端——云端 OpenAI 兼容 / Anthropic（openai 兼容格式）/ 本地（LM Studio/Ollama/vLLM = OpenAI 兼容 endpoint）。不凭空新增。
- `generate_config(...) -> dict`：按选择拼配置 dict（api_key 用 `${VAR}` 或本地占位）。
- `default_env_var(provider_kind) -> str`：openai→OPENAI_API_KEY、anthropic→ANTHROPIC_API_KEY…
- `check_env_var(name) -> bool`：`os.environ` 查。
- `check_endpoint(base_url, timeout=3) -> (ok, detail)`：GET `{base_url}/models`，本地 host 关代理，捕获超时/连接失败。
- `write_config(path, data, backup=True)`：已存在则备份；`yaml.safe_dump`。
- `validate_generated(path)`：调 `load_config(path)` 确认能解析、active 存在。

**配置生成的 schema**：不变（用现有 AppConfig）。生成的 config.yaml 形如：
```yaml
active: cloud
providers:
  cloud:
    model: openai/gpt-4o
    api_key: ${OPENAI_API_KEY}      # 只存变量名
    # 云端无 api_base
  # 或本地：
  # local:
  #   model: openai/local-model
  #   api_key: "lm-studio"
  #   api_base: http://localhost:1234/v1
```
**与现有加载兼容**：生成物就是现有格式，load_config 直接可用；`${VAR}` 由现有展开处理。

**错误处理**：端点检测失败 → 警告但允许继续（用户可能稍后启动 LM Studio）；写文件失败 → 报错退出；
校验失败 → 提示具体问题。全程不崩、不吞错。

**跨平台**：路径用 pathlib；无回显用 `getpass`（跨平台）；环境变量设置**指引**分平台打印
（PowerShell `setx`/`$env:` vs bash `export`）——只打印，不代改。

**非交互模式（M5 可选，先评估）**：
`init --provider openai --model gpt-4o --api-key-env OPENAI_API_KEY [--yes]`。
先做交互;非交互作为可选增强，接口预留。

## 6. 文件修改计划
| 文件 | 改动 | 动内核？ |
|------|------|:---:|
| `cli/init.py` | 新增：向导/生成/env 检测/endpoint 检测/校验 | 否（cli 层） |
| `main.py` | 注册 `init` 命令，调用 cli.init.run_init | 否 |
| `config/schema.py` | **不改**（复用 ${VAR}） | — |
| `config/loader.py` | **不改** | — |
| `tests/test_init.py` | 新增：生成/已存在备份/env 检测/脱敏/校验/endpoint mock | — |
| README/ROADMAP | 同步 init 用法与完成状态 | — |

## 7. 测试计划
**单元**：
- generate_config：云端→`${VAR}`、本地→占位 key+base_url、字段正确
- write_config：已存在 → 生成 .bak、不静默覆盖；取消路径
- check_env_var：已设/未设
- key 脱敏：确认生成物与日志里无明文 key（断言 config 内容是 `${VAR}` 而非真实值）
- provider/model 校验：非法选择被拒
**CLI（交互）**：monkeypatch 输入序列 → 生成预期 config.yaml；已存在 → 备份/取消分支
**endpoint 检测**：mock httpx → 可连通 / 超时 / 连接失败 三态
**集成验收**：init 生成 config（本地端点或 mock provider）后，`run` 能装配成功（不打真实 API）
**回归**：现有 config.yaml 仍加载正常；run/chat/sessions/providers 不受影响；现有测试全绿

## 8. 验收标准（可测试项）
1. **新机器 init 生成可用 config.yaml**：
   - 字段：active + 至少一个 provider（model 必填；云端含 `api_key: ${VAR}`；本地含 api_base + 占位 key）
   - 位置：cwd 下 `./config.yaml`
   - 已存在：备份 `config.yaml.bak` 后覆盖，或用户选取消则不动
2. **本地端点检测**：
   - 成功：提示"✅ 已连接，检测到 N 个模型"（若能列）
   - 失败：提示"连接失败：<原因>，可稍后启动后再试"，允许继续
   - 超时：3s 内返回超时提示，不卡死
3. **生成后能跑通 run**：
   - smoke test：init 后 `load_config` + `_build_client` 成功装配（不发真实请求）
   - 防泄露：测试断言 config.yaml 内容为 `${VAR}`，从不写真实 key；provider 用 mock/本地
   - mock：endpoint 检测 mock httpx；run 装配只验证到 client 构建，不打网络

## 9. 风险与暂缓
**暂缓**（明确不做）：自动写 shell profile、自动注册系统环境变量、GUI、多 profile 管理、云端账号登录、密钥管理器集成。
**风险**：
- ⚠️ 绝不明文写 key：默认不读 key，只写 `${VAR}` + 检测变量是否设。
- ⚠️ 已有 config 必须备份，不静默覆盖。
- ⚠️ 端点检测必须带 timeout（本地 host 关代理），否则可能卡住。
- ⚠️ 无回显输入在个别终端不可用 → 明确提示并不接收 key。
- ⚠️ cli/init 的 endpoint 检测涉及网络：测试必须 mock，不打真实端点。
