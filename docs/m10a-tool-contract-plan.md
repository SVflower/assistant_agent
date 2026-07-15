# M10a 计划：工具契约与大文件/大输出工程

> 状态：待用户审阅。上位规划见 `docs/phase3-trustworthy-agent-plan.md`。
> 本里程碑不修改 `agent/loop.py`。

## 1. 目标

把目前适合小型演示的工具 I/O 提升为适合真实编码任务的稳定契约：参数在副作用前统一校验，
错误可机器判断且仍对模型可读；大文件可按范围读取和检索；Shell/Git 输出在来源端有界；写入采用
同目录临时文件和原子替换；MCP 的结构化结果不再被丢弃。完成后还清 D16。

## 2. 调研结论

本期参考公开接口提炼原则，不复制完整框架：

1. [LangChain ToolMessage artifact](https://reference.langchain.com/python/langchain-core/messages/tool/ToolMessage/artifact)
   明确区分“发送给模型的精简 content”和“供程序其余部分使用的完整 artifact”。本项目采用相同边界，
   但 artifact 也必须有大小和数量硬上限，不能把内存无界增长转移为磁盘无界增长。
2. [OpenAI Agents SDK 工具生命周期](https://github.com/openai/openai-agents-python/blob/main/.agents/references/tool-execution-lifecycle.md)
   要求在副作用前完成工具查找、参数验证和 guardrail；工具局部失败与父运行取消是不同语义。
   本期只落地前者，不借机异步化或实现取消。
3. [OpenAI Agents SDK FunctionTool](https://openai.github.io/openai-agents-js/openai/agents/type-aliases/functiontool/)
   将参数 schema、timeout、错误转模型结果、SDK 私有 custom data 分开。本项目据此让
   `ToolResult` 同时保留模型可见文本和结构化 code/metadata/artifact。
4. [MCP Tools 规范](https://modelcontextprotocol.io/specification/draft/server/tools) 支持
   `outputSchema` 与 `structuredContent`；客户端不能只拼 text block，否则合法结构化成功结果会变成
   “无内容”。本期保留并序列化结构化结果，同时继续兼容旧 content。
5. [Python subprocess 文档](https://docs.python.org/3.11/library/subprocess.html) 说明
   `capture_output=True` 会把 stdout/stderr 接到 PIPE，并由 `communicate()` 完整收集；这不适合无界输出。
   Shell/Git 改用 `Popen` + 并发 drain + 有界 collector，超限后继续丢弃读取，防管道阻塞。
6. Python [`os.replace`](https://docs.python.org/3.11/library/os.html#os.replace) 的可靠文件更新模式是
   “同目录临时文件 -> flush/fsync -> replace”；同目录保证同一文件系统，
   替换前失败时旧文件保持不变。目录 fsync 仅在平台支持时尽力执行。

共同原则：模型上下文、程序元数据、持久 artifact 是三个不同载荷；输入先验证再授权/执行；限量要在
数据来源处执行；错误码服务程序判断，错误文本服务模型恢复；跨平台行为必须以 Windows/Linux CI 验证。

## 3. 现状评估

### 可复用基础

- `ToolRegistry.execute()` 已是所有内置、Skill、MCP 工具的强制入口，适合统一参数校验。
- `ToolResult(output, is_error)` 已贯穿 Loop/UI/observer，可增量扩展而不改内核。
- `ToolContext.max_output_chars` 与任务累计预算仍作为“进入上下文前”的最后封套。
- Session 已有同目录临时文件、fsync、`os.replace` 的成熟实现，可提炼原则但不直接形成反向依赖。
- M9c scripted eval 能为分页、结构化错误和 artifact 轨迹增加行为回归。

### 当前缺口

- JSON Schema 目前只展示给模型，`read_file(path=123)` 会进入工具并靠异常兜底。
- `read_file` 用 `read_text()` 读全文件，只能返回前 10 万字符，无法请求后续范围。
- `code_search` 整文件载入，不能返回上下文行；`list_dir` 对超大目录也先构造完整列表。
- Shell/Git 使用 `subprocess.run(capture_output=True)`，Registry 截断发生在完整输出进入内存之后。
- write/edit/multi_edit 直接 `write_text()`；进程中断可能留下半文件，且 Windows 下可能改变 CRLF。
- `ToolResult` 没有稳定错误码、retryable、metadata 或 artifact 引用。
- MCP adapter 只提取 text block，忽略 `structuredContent`/`outputSchema`。

结论：主要改动位于 `tools/`、`mcp/tool.py`、配置和 Runtime 装配；不需要修改 `agent/loop.py`。

## 4. 范围

### 必做

1. 扩展兼容的 `ToolResult`：`code`、`retryable`、`metadata`、`artifacts`，保留现有字段和构造方式。
2. Registry 在权限判断和 Tool.run 前按 JSON Schema 校验参数，给稳定 `invalid_arguments` 结果。
3. `read_file` 支持 1-based `start_line`/`end_line`，返回实际范围、总行数、`has_more` 和下一页提示。
4. `code_search` 支持 0-10 行上下文，按流式行扫描；`list_dir` 增加结果上限和截断提示。
5. 文件 write/edit/multi_edit 统一走原子写 helper，保留原文件权限和换行字节风格。
6. Shell/Git 统一使用有界进程捕获器；stdout/stderr 并发 drain，超限仍排空但不继续占内存。
7. 大进程输出只把 head/tail preview 送模型；受限完整输出写入 workspace 内 artifact，并返回引用。
8. artifact 有单文件字符上限、目录 confinement、原子保存和保留数量上限；超过上限明确标记不完整。
9. MCP 同时处理 text、非文本占位与 `structuredContent`，结构放 metadata，必要时给模型 JSON 摘要。
10. 拆分 `file_ops.py`：保留兼容 re-export facade，读/浏览、写/编辑、共享 I/O 各自单一职责。
11. 更新 config.example、README、eval case、技术债和状态文档；全量 DoD 通过。

### 可选

- Git/Shell artifact 提供 `read_file` 可直接读取的普通 UTF-8 文件，不新增专用 read_artifact 工具。
- 对超长单行给行内 head/tail，而不是把整行放进内存或上下文。
- metadata 中记录 scanned/returned/dropped chars，供 observer 与后续 eval 使用。

### 不做

- 不新增 Git 写操作，不扩大权限能力。
- 不实现二进制编辑、LSP、完整 patch 引擎或 mmap 优化。
- 不承诺保存真正无限的完整输出；超过 artifact 硬上限后继续 drain 并丢弃，明确 `complete=false`。
- 不把 artifact 内容写入 Session JSON；会话只保留模型可见 preview/引用。
- 不异步化 Tool/AgentLoop，不做 Ctrl+C 中途取消；留给 M10c 决策。
- 不修改 `agent/loop.py`。

## 5. 契约设计

```python
@dataclass(frozen=True)
class ArtifactRef:
    id: str
    path: str                 # workspace 内相对路径
    media_type: str = "text/plain"
    size_chars: int = 0
    complete: bool = True

@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    code: str = "ok"
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    budget_exhausted: str | None = None
    executed: bool = True
```

兼容规则：现有 `ToolResult(output="x", is_error=True)` 仍合法；`ok()` 默认 `code=ok`；`error()` 要求
调用点逐步填写稳定 code。Registry 截断、权限拒绝和预算结果复制时必须保留所有结构化字段。

首批通用错误码：`invalid_arguments`、`not_found`、`not_file`、`not_directory`、`decode_error`、
`ambiguous_edit`、`no_match`、`io_error`、`timeout`、`process_start_failed`、`permission_denied`、
`budget_exhausted`、`tool_exception`、`mcp_transport_error`、`mcp_tool_error`。code 是稳定机器接口，
中文 message 可优化；retryable 只表示“修正参数/环境后可重试”，不表示框架会自动重放。

## 6. 参数校验

- 显式新增运行时依赖 `jsonschema>=4.23,<5`，使用 Draft 2020-12 validator；不手写半套 schema parser。
- Registry 注册时检查 schema 自身合法性；执行时先检查工具存在，再校验 args，再进入权限策略。
- `required/type/enum/minimum/maximum/minItems` 由 schema 处理；跨字段规则由工具返回同一
  `invalid_arguments` code。
- 现有 schema 默认不强制 `additionalProperties=false`，继续容忍小模型附带无害字段；敏感工具可单独收紧。
- 外部 MCP 的坏 inputSchema 不得拖垮整个 Runtime：该工具跳过并生成 manager warning。

## 7. 大文件与搜索

`read_file` 默认行为对小文件保持原文本兼容；显式范围或触发默认上限时返回：

```text
[lines 2001-4000 of 100000, has_more=true]
...
[next: read_file(path=..., start_line=4001, end_line=6000)]
```

- 默认最多 2000 行且仍受 100000 字符来源上限；范围最大 5000 行，非法/反向范围先返回结构化错误。
- 逐行扫描并只保留请求区间；为得到 total_lines 可以扫描文件尾，但内存与返回大小保持有界。
- 超长单行只保留 head/tail 并标记；UTF-8 解码失败稳定返回 `decode_error`。
- `code_search(context_lines=N)` 使用前置 ring buffer + 有界后续行，重叠块合并；最大 500 个 match。
- `list_dir(max_results)` 迭代计数并有界保留，不先建立无限 entries；排序只保证保留集合稳定。

## 8. 有界进程与 Artifact

新增 `tools/process.py` 和 `tools/artifacts.py`：

1. `Popen` 分别连接 stdout/stderr；两个 reader 线程按固定 chunk drain，主线程负责 timeout/wait。
2. collector 只保留配置的最大字符数和 head/tail preview；达到硬上限后继续读取并计 dropped 数。
3. 两个 reader 必须在正常、timeout、异常路径 join；timeout 后 terminate/kill/wait，避免僵尸进程。
4. 超过 inline 限制才持久化 artifact；同目录临时写 + `os.replace`，文件名随机且不可由模型指定。
5. artifact 默认根为 `<workspace>/.assistant_agent/artifacts`，所有路径 resolve 后 confinement。
6. 新配置建议：`tools.max_captured_output_chars=1000000`、`tools.max_artifact_files=100`；即使
   `max_output_chars=0`，来源捕获硬上限仍生效。
7. Shell 保留合并后的退出码/stdout/stderr标签；Git 复用同一 helper 且继续 `shell=False` 白名单。

当前 MCP SDK 已在返回对象构造时接收完整响应，客户端 adapter 无法真正做到来源端限量；本期只在
adapter 边界立刻投影/截断并建议 server 使用分页/outputSchema。不能把这点误报为 MCP 内存硬保证。

## 9. 文件原子写

新增 `tools/file_io.py`：

- 读取采用 `open(..., newline="")`，编辑后保持 `\n`/`\r\n` 原字节风格。
- 临时文件创建在目标同目录，权限默认继承已有目标；写入后 flush + `os.fsync`。
- `os.replace(temp, target)` 作为提交点；替换前任意异常删除 temp，旧目标不变。
- POSIX 下尽力 fsync 父目录；Windows 不支持目录 fsync 时不把其当失败。
- write_file 新建父目录仍保留现状；目录创建不是原子事务，文档明确。
- multi_edit 的“逻辑原子”与“磁盘替换原子”同时成立；多文件事务仍不在范围内。

## 10. MCP 结构化结果

- manager 发现工具时保存可选 `outputSchema`。
- adapter 提取 `content` 和 `structuredContent`；有 text 时 text 为主，无 text 时使用稳定 JSON 文本。
- `ToolResult.metadata["structured_content"]` 保留结构化对象，`output_schema` 只记录指纹/是否存在，
  不重复大 schema。
- `isError=true` 返回 `mcp_tool_error`；timeout/连接异常分别为稳定 transport code。
- 非文本 image/audio/resource 仍只给占位与类型 metadata，本期不把 base64 灌进模型上下文。

## 11. 文件与依赖调整

- `tools/base.py`：ArtifactRef/ToolResult 兼容扩展、ToolContext artifact 配置。
- `tools/validation.py`：jsonschema 编译、schema/args 错误格式化。
- `tools/registry.py`：注册时 schema 检查、执行前验证、结构化字段完整传递。
- `tools/file_io.py`：保换行读取、原子写、路径 helper。
- `tools/file_read.py`：ReadFileTool/ListDirTool。
- `tools/file_edit.py`：Write/Edit/MultiEdit。
- `tools/file_ops.py`：仅兼容 re-export，旧 import 不破坏。
- `tools/process.py`、`tools/artifacts.py`：有界捕获和 artifact store。
- `tools/shell.py`、`tools/git.py`、`tools/search.py`、`mcp/tool.py`、`mcp/manager.py`：迁移。
- `config/schema.py`、`cli/setup.py`、`config.example.yaml`：新上限装配。
- `pyproject.toml`：显式加入 jsonschema 兼容范围。

拆分依据是职责和测试隔离，不以 300 行作为机械目标；`file_ops.py` 当前同时负责读、写、编辑、目录和
共享 helper，已经形成真实拆分收益。

## 12. 实施顺序

1. P1 契约：ToolResult/ArtifactRef、jsonschema validator、Registry 字段保真和错误码测试。
2. P2 文件：file_ops 拆分、范围读取、目录上限、原子写、换行保持和 10 万行 fixture。
3. P3 搜索：上下文行、流式扫描、结果上限和重叠块测试。
4. P4 进程：有界 collector、Shell/Git artifact、双 PIPE/timeout/超限测试和配置。
5. P5 MCP：structuredContent/outputSchema、错误码和大载荷投影。
6. P6 eval/文档：新增行为案例，全量 DoD、D16/状态同步、计划归档与提交。

每个批次单独保持 pytest/Ruff/mypy 可运行，不把所有工具一次性迁移后再修。

## 13. 测试计划

- Validation：required/type/enum/range/array、坏 MCP schema、未知工具、验证发生在权限/副作用前。
- ToolResult：旧构造兼容；Registry 截断/权限/预算后 code、metadata、artifact 不丢。
- Read：空文件、小文件兼容、首/中/尾页、越界、10 万行、超长单行、CRLF、二进制。
- Atomic write：write/edit/multi_edit 成功；替换前故障注入旧文件不变、temp 清理、权限/换行保持。
- Search/List：上下文 0/边界/重叠、坏 regex、最大结果、超大目录和大文件内存有界。
- Process：stdout/stderr 同时超管道容量不死锁；head/tail、dropped 计数、artifact cap、timeout kill/join。
- Artifact：路径 confinement、随机名、原子失败、保留数 pruning、报告/Session 不内联完整载荷。
- Git：大 diff 有界、白名单与参数解析不回退、非仓库错误仍交模型。
- MCP：text-only、structured-only、两者并存、非文本、isError、timeout、坏 schema。
- Eval：大文件中段读取、坏参数恢复、大输出 artifact、原子编辑至少 4 个 deterministic case。
- 全量：pytest/coverage、Ruff format/check、mypy、架构测试、scripted eval。

## 14. 验收标准

1. 能读取 10 万行文件的任意范围，模型上下文只收到请求页和下一页提示。
2. Shell/Git 生成远超 PIPE/inline 上限的 stdout+stderr 时不死锁、内存保留有硬上限。
3. 超大输出有受限 artifact 引用；artifact 超硬线明确 `complete=false`，目录不会无限增长。
4. 所有内置工具的 schema 在副作用前执行；典型错误有稳定 code/retryable。
5. write/edit/multi_edit 故障注入证明替换前失败时旧文件不变，CRLF 不被无意改写。
6. MCP structured-only 成功结果不再显示“无内容”，结构可由 metadata/JSON 文本取得。
7. `file_ops.py` 拆分后旧 import 保持兼容，权限与预算行为不回退。
8. D16 标记还清；状态数字来自最终实测；无密钥、artifact、临时文件入库。
9. 不修改 `agent/loop.py`；M9c 的 303 passed 基线不回退。

## 15. 风险与控制

- **契约扩展波及面大**：保持旧字段和构造器；先改 Registry 字段保真，再逐工具补 code。
- **validator 让笨模型失败更多**：错误返回精确 JSON path/expected/actual 且 retryable，不自动猜测或改参数。
- **双 PIPE 死锁/线程泄漏**：独立 reader + 所有路径 join；压力测试 stdout/stderr 同时超过系统管道。
- **artifact 泄密/膨胀**：仅 workspace 内、gitignore、权限沿用当前用户、单文件/数量双硬线；README 明示。
- **原子替换平台差异**：同目录 temp；Windows/Linux CI 故障注入；目录 fsync 只做支持平台的增强。
- **分页精确总行数成本**：允许 O(file size) 扫描换取 O(page size) 内存；若性能实测不足再引入索引，不预建缓存。
- **MCP 无来源端硬限**：明确边界，不宣称 adapter 截断能阻止 SDK 接收大响应；服务端分页仍是根治方案。
- **范围膨胀**：不做取消/异步/多文件事务；这些分别留给 M10c 或未来需求。
