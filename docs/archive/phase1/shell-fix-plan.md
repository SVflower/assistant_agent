# shell 工具修复方案

> bug：让 agent「查看今天星期几」调用 shell 工具失败/卡死 60 秒。
> 状态：待你审阅，暂未动代码。

## 一、问题复现与根因

让模型执行「今天星期几」，它调用 `run_shell`，命令为 `date "+%A"`（Unix 语法）。
在 Windows 上经 `cmd.exe` 执行时卡死，直到 60 秒超时。

实测复现（`subprocess.run('date', shell=True, ...)`）确认了根因，共**三个独立问题**：

### 问题 1：stdin 未重定向 → 交互式命令阻塞（核心）
`cmd` 的 `date` 命令打印完当前日期后，会停下来**等用户输入新日期**：
```
当前日期: 2026/07/01 周三
输入新日期: (年月日)     ← 卡在这里等 stdin
```
我们的 `subprocess.run` 没有重定向 stdin，命令拿不到输入就一直阻塞到超时。
这是**通用隐患**，不止 `date`——`more`、`pause`、`sort`（无参）、`set /p` 等交互式命令都会中招。

> **实测验证（关键，且有反复）**：
> - 从 Git Bash 启动时，父进程 stdin 是管道（立即 EOF），`date "+%A"` 0.03s 就失败返回，**无法复现阻塞**。这一度让人误以为 stdin 不是主因。
> - 用 `Popen(stdin=PIPE)` 保持一个**开放不关闭的 stdin**（模拟交互式控制台），`date "+%A"` **卡住 6s+**，复现了用户环境的 60s 超时。
> - 加 `stdin=subprocess.DEVNULL` 后，同一命令 **0.02s 立即返回**。
> 结论：真因是**命令在有开放 stdin 时停下等输入**；是否阻塞取决于父进程 stdin 来源（bash 管道 vs 交互终端）。`DEVNULL` 无论父进程如何都切断 stdin，修复有效。

### 问题 2：模型不知道运行在 Windows → 用错命令语法
系统提示词没告知操作系统，模型默认按 Unix 习惯输出 `date "+%A"`。
即便修好 stdin，模型仍会用错命令，只是不再卡死而已（会快速失败）。

### 问题 3：输出编码错误 → 中文乱码
`cmd` 输出是 GBK 编码，我们用 `text=True`（默认 UTF-8）解码，
导致中文变乱码：`当前日期` → `��ǰ����`。

## 二、修复方案

### 修复 1：stdin 重定向 + 进程组隔离（改 `tools/shell.py`）
`subprocess.run` 增加 `stdin=subprocess.DEVNULL`。
交互式命令读不到输入会**立即失败退出**，而不是阻塞到超时。

### 修复 2：系统提示词注入运行环境（改 `agent/prompts.py` + `agent/loop.py`）
在系统提示词里动态注入当前操作系统信息，例如：
```
当前运行环境：Windows（命令通过 cmd.exe 执行）。
请使用 Windows 命令语法，不要用 Unix 专有命令。
优先用跨平台方式（如需要日期时间，可用 Python）。
```
让 `platform.system()` 决定注入内容，保证 macOS/Linux 上也正确。

### 修复 3：输出编码容错（改 `tools/shell.py`）
不再用 `text=True`，改为捕获 bytes 后按平台编码解码：
- Windows：先试 UTF-8，失败回退 GBK（`errors="replace"` 兜底不崩）
- 其他平台：UTF-8

### 附带增强：超时提示更明确
超时错误信息里提示可能是交互式命令，引导模型换非交互写法。

## 三、涉及文件与内核铁律

| 文件 | 改动 | 是否动内核 |
|------|------|-----------|
| `tools/shell.py` | stdin 重定向 + 编码容错 | 否（工具层）|
| `agent/prompts.py` | 增加运行环境说明 | 否（提示词）|
| `agent/loop.py` | 把 OS 信息传入提示词 | **轻微动内核** |

> 铁律第 4 条「内核保持封闭」：`loop.py` 只加「构造系统提示词时带上环境信息」，
> 不改循环控制流。属于可接受的最小改动。若你希望完全不碰 loop.py，
> 可改为在 `Conversation`/`prompts` 层用 `platform.system()` 自取，届时 loop.py 零改动。

## 四、测试计划（铁律第 5 条：新功能带测试）

1. `test_shell_no_stdin_hang`：交互式命令（如无参 `sort` 或 `date`）能在超时前返回，不卡满 60s
2. `test_shell_stdin_devnull`：验证 stdin 被重定向（读 stdin 的命令得到 EOF）
3. `test_shell_encoding_fallback`：GBK 输出不崩、不乱码（mock 或造 bytes）
4. `test_prompt_includes_os`：系统提示词包含当前操作系统名
5. 保证现有 22 个测试仍全绿

## 五、验收标准

修复后手动跑：
```
assistant-agent run "今天星期几"
```
预期：**不卡死**；模型用正确方式拿到日期并回答；中文不乱码。

## 六、与流式优化的先后

先修此 bug（地基），再做流式（体验）。理由：
流式会改 `AgentLoop` 内核，带 bug 改内核会让问题难以归因。
先让基础工具可靠，再在稳固地基上做流式。
