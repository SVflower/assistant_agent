# 01 项目里会遇到的 Python

本章只讲阅读本项目必须掌握的 Python。示例经过缩短，但保留了真实代码的写法。

## 类型标注不是运行时强制类型

```python
def get(self, name: str) -> Tool | None:
    return self._tools.get(name)
```

- `name: str` 表示调用者应该传字符串。
- `-> Tool | None` 表示结果可能是 `Tool`，也可能不存在。
- Python 默认不会仅凭标注阻止错误类型，项目使用 mypy 做静态检查，并在外部输入边界用 Pydantic/
  JSON Schema 做运行时校验。

常见容器标注：

```python
list[Tool]                 # 有顺序、可变的 Tool 列表
dict[str, Any]             # 字符串键，值类型未知
tuple[RuntimeNotice, ...]  # 不可变、任意长度的同类元组
Callable[[str], bool]      # 接收 str、返回 bool 的函数
```

`Any` 的意思是“这里暂时放弃静态类型检查”，不是“任何值都一定安全”。来自模型、YAML、JSON 的
`Any` 必须在边界校验。

## dataclass：以数据为主的普通对象

```python
@dataclass(frozen=True)
class RunExecution:
    run_id: str
    events: Iterator[ItemEvent]
```

`@dataclass` 自动生成构造函数等样板代码。`frozen=True` 防止字段被重新赋值，适合表示已经创建好的
结果句柄。它并不会让字段内部对象也自动不可变，例如 `events` 指向的迭代器仍然有状态。

## Pydantic BaseModel：需要验证和序列化的数据

Run checkpoint、配置和公共 DTO 需要从 JSON/YAML 恢复，因此使用 Pydantic：

```python
class RunState(StrictStateModel):
    schema_version: Literal[6] = 6
    status: RunStatus
```

`model_validate(raw)` 会验证外部字典，`model_dump(mode="json")` 会生成可持久化结构。`Literal[6]`
表示这里只允许数字 `6`。相比 dataclass，Pydantic 更适合“不可信输入 -> 已验证对象”的边界。

## Protocol：面向能力，而不是具体实现

```python
class SessionRepository(Protocol):
    def load(self, session_id: str) -> Session: ...
```

任何拥有兼容 `load` 方法的对象都能满足这个 Port，不必继承 `SessionRepository`。因此 Application
可以依赖抽象，生产环境注入文件 Store，测试注入 Fake Store。这叫结构化子类型，也常被称为
duck typing 的类型化版本。

## Iterator 与 yield：边生成边消费

```python
def run(...) -> Iterator[ItemEvent]:
    yield ItemEvent(kind="activity", phase="calling_model")
    yield from execute_turn(...)
```

包含 `yield` 的函数被调用时不会立刻执行完整函数，而是返回 generator。消费者每次调用 `next()`，
函数运行到下一个 `yield` 暂停。好处是模型流式片段和工具进度可以立即交给 UI，不必等待任务结束。

重要后果：

- generator 中的异常通常在迭代时发生，而不是创建时发生。
- 消费者提前停止时应调用 `close()`，让 `finally` 释放 lease 和 Runtime 资源。
- 同一个 generator 不能由多个线程并发 `next()`。

## context manager 与 with

```python
with build_runtime(...) as runtime:
    ...
```

对象的 `__enter__` 在进入时运行，`__exit__` 在正常结束或抛异常时都会运行。本项目用它保证 Runtime、
RunExecution、文件锁按时关闭。`contextlib.contextmanager` 则允许用一个带 `yield` 的函数实现同样协议。

## try / except / finally

```python
resource = create_resource()
try:
    use(resource)
finally:
    resource.close()
```

`finally` 无论成功、异常还是提前 `return` 都执行。Agent 中最重要的用途是释放 Session lease、停止
子进程和关闭 MCP。`except BaseException` 比 `except Exception` 范围更宽，也会捕获取消类异常；只有
资源回滚边界才适合这样写，业务错误通常捕获更具体的异常。

## 属性、私有命名与 property

- `self.name`：实例字段。
- `self._name`：约定为内部实现，Python 不会真正禁止外部访问。
- `@property`：让方法以字段形式读取，例如 `runtime.closed`。
- `@staticmethod`：方法不读取实例状态，只是放在该类命名空间中的相关函数。

## 线程、锁和 async

本项目核心执行保持同步，但可能由 API 放到工作线程运行。`threading.Lock` 保护同一进程里的共享状态；
文件锁和 execution lease 处理跨进程竞争。两者不是一回事。

MCP SDK 是异步的。`MCPManager` 在独立线程中维护 asyncio event loop，再把同步工具调用提交进去。
这样不会迫使 Agent Loop 和所有工具改成 async。不要在并发请求中使用全局 `os.chdir()`，因为工作目录
是进程全局状态；项目把 workspace/cwd 显式传入。

## 下一步

阅读源码时先识别一个对象是 dataclass、Pydantic DTO、Protocol 还是有生命周期的服务对象。这个判断
会告诉你：它负责保存数据、校验边界、描述能力，还是拥有需要关闭的资源。

