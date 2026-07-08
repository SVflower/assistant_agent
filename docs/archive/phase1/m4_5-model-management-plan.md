# M4.5 实施方案 — 模型管理与切换

> 目标：模型切换从"改 config.yaml + 重启"升级为启动标志 + 对话内命令 + 列表菜单。
> 状态：待审阅，未动代码。用户已批准"轻碰内核"（加换 client 方法，不改控制流）。
> 最后更新：2026-07-02

---

## 一、结论

数据层已完整（多 provider 列表、本地/云端统一、环境变量认证、`active` 持久默认），
**缺的只是"切换的便捷入口"**。M4.5 补三个入口，复用已有能力（questionary 菜单）：
启动标志 `--provider`、对话内 `/model` 命令、provider 列表。

## 二、参考借鉴（成熟产品）

Claude Code 的三层切换：对话中 `/model`（交互菜单即时切）、启动 `--model` 标志、
配置/环境变量持久默认。强调"按任务复杂度换模型"。LiteLLM 生态：多 provider 命名列表、
key 走标准环境变量、本地/云端同一抽象、运行时可切。

对照现状：数据层已全有；缺启动标志、对话内切换、列表菜单三个入口。

## 三、范围

### 必做
1. **`--provider/-p` 启动标志**：`run`/`chat` 临时指定用哪个 provider（覆盖 config.active），不改文件。
2. **对话内 `/model`**：chat 里输入 `/model` 弹方向键菜单（复用 questionary）列出所有 provider，
   选一个当场切换；`/model <名>` 直接切。切换**保留当前对话历史**。
3. **provider 列表**：`/model` 菜单即列表；另可 `assistant-agent providers` 命令列出（名字/模型/云端或本地）。

### 可选
- `/model` 菜单里显示每个 provider 是"云端/本地"（看 api_base 有无）。

### 不做
- 运行时新增/编辑 provider（仍改 config.yaml）——避免把 CLI 变成配置编辑器。
- 自动按成本/复杂度路由、fallback 链——过重，非本期。

## 四、技术设计

### 内核改动（轻碰 AgentLoop，已获批准）
`AgentLoop._client` 是实例属性、`_conversation` 独立持有。切模型只需替换 client，
历史天然保留。新增方法：
```
AgentLoop.set_client(client: LLMClient) -> None:
    self._client = client   # 仅换客户端；_conversation 不动，上下文保留
```
**不改 `run()` 控制流**——只加一个 setter。守铁律：控制流封闭，仅换依赖。

### provider 切换的装配（main）
- 抽出 `_build_client(provider_config) -> LLMClient`（现有 _setup 里的逻辑）。
- `--provider` 覆盖：加载 config 后，若指定了 `-p X` 且 X 在 providers 中，则 active=X（否则报错列出可选）。
- `/model` 切换：在 chat 循环拦截 `/model` 输入 → 选出新 provider → `loop.set_client(_build_client(新))`
  + 更新 banner/提示，历史保留。

### `/model` 命令（chat 内特殊输入）
- chat 循环里，输入以 `/` 开头当作命令拦截，不进 ReAct：
  - `/model`：questionary 菜单列出所有 provider（当前 active 标记），选中即切。
  - `/model <名>`：直接切；名字非法则提示可选列表。
  - 复用 `Console.ask_question` 或直接用 questionary select。
- 非交互（无 tty）下 `/model` 无参：提示"请用 /model <名>"，不阻塞。

### schema / 输入输出
- CLI：`--provider/-p TEXT`（run、chat 都加）；`providers` 子命令（无参，列出）。
- 对话内命令：文本协议，`/model` 或 `/model <名>`。

### 权限
- 切模型是本地配置层操作，无副作用、不触发确认。

### 错误处理
- `-p 非法名` → 报错并列出可用 provider，退出（run）或提示（chat）。
- `/model 非法名` → 提示可用列表，不切换、不崩。

### 流式事件展示
- 切换后打印一行确认（新 provider/model）；不新增事件类型。

## 五、涉及文件
| 文件 | 改动 | 动内核？ |
|------|------|:---:|
| `agent/loop.py` | 加 `set_client`（仅换 client，控制流不变） | **是（轻碰，已批准）** |
| `main.py` | `--provider` 标志、`/model` 拦截、`providers` 命令、抽 `_build_client` | 否 |
| `ui/console.py` | provider 菜单/列表展示（复用 ask/questionary） | 否 |
| `config/schema.py` | 无需改（providers/active 已有） | 否 |
| `tests/test_loop.py` | set_client 保留历史的测试 | — |
| `tests/test_main*.py` | provider 选择/非法名（可按需） | — |

## 六、开发计划（每步带测试）
1. `AgentLoop.set_client` + 单测：切 client 后 `export_history()` 不变（历史保留）
2. main 抽 `_build_client`；`--provider` 标志 + 非法名报错 → 测试
3. chat 内 `/model` 拦截 + 菜单/直切 + 切换后 banner → 手动验证（交互）
4. `providers` 列出命令
5. DoD：pytest + ruff + 架构测试全绿；文档同步（README、ROADMAP、CLAUDE.md 命令）

## 七、验收标准
1. `run/chat --provider <名>` 用指定 provider（不改 config）；非法名报错列出可选
2. chat 内 `/model` 弹菜单选择、`/model <名>` 直切，**切换后对话历史保留**（模型记得前文）
3. `providers` 列出所有 provider（名/模型/云端或本地）
4. 切换无副作用、不触发危险确认
5. `set_client` 有单测证明历史保留；现有测试不回退；ruff + 架构测试通过
6. 内核仅新增 setter，`run()` 控制流未改

## 八、风险与边界
- ⚠️ 内核只加 setter，绝不动 run() 控制流；改完确认现有循环测试全绿。
- ❌ 不做运行时编辑/新增 provider（仍靠 config.yaml）——不把 CLI 变成配置编辑器。
- ❌ 不做成本路由/自动 fallback——过重，非本期。
- ⚠️ 切模型保留历史：新模型可能上下文窗口更小 → 已有 token 感知截断兜底（M3），天然安全。
- ⚠️ `/model` 非交互环境无参时不阻塞，提示用带名形式。
