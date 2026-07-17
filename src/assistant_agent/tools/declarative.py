"""把带类型注解的 Python 函数适配为标准 Tool。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, TypeAlias, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import PermissionRequest

ToolFunction: TypeAlias = Callable[..., Any]
PermissionResolver: TypeAlias = Callable[[dict[str, Any], ToolContext], list[PermissionRequest]]


class FunctionTool(Tool):
    """将一个普通函数包装进现有 Registry 安全链路。"""

    def __init__(
        self,
        function: ToolFunction,
        *,
        name: str | None = None,
        description: str | None = None,
        permissions: PermissionResolver | None = None,
    ) -> None:
        if inspect.iscoroutinefunction(function):
            raise TypeError(
                f"工具 {function.__name__} 不支持异步函数；当前 Registry 仅支持同步 Tool"
            )
        self._function = function
        self.name = name or function.__name__
        self.description = (
            description if description is not None else inspect.getdoc(function) or ""
        )
        self._permissions = permissions
        self._context_parameter, self._arguments_model = _build_arguments_model(function)
        self._parameters = self._arguments_model.model_json_schema()

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        if self._permissions is None:
            return super().permission_requests(args, ctx)
        return self._permissions(self._validated_args(args), ctx)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            kwargs = self._validated_args(args)
        except ValidationError as exc:
            return ToolResult.error(
                f"声明式工具 {self.name} 参数校验失败：{_validation_message(exc)}",
                code="invalid_arguments",
                retryable=True,
                executed=False,
            )
        if self._context_parameter is not None:
            kwargs[self._context_parameter] = ctx
        try:
            value = self._function(**kwargs)
        except Exception:
            return ToolResult.error(
                f"声明式工具 {self.name} 执行异常",
                code="tool_exception",
            )
        if isinstance(value, ToolResult):
            return value
        if isinstance(value, str):
            return ToolResult.ok(value)
        return ToolResult.error(
            f"声明式工具 {self.name} 返回了不支持的类型：{type(value).__name__}",
            code="invalid_tool_result",
        )

    def _validated_args(self, args: dict[str, Any]) -> dict[str, Any]:
        validated = self._arguments_model.model_validate(args)
        return validated.model_dump(mode="python")


@overload
def agent_tool(function: ToolFunction, /) -> FunctionTool: ...


@overload
def agent_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: PermissionResolver | None = None,
) -> Callable[[ToolFunction], FunctionTool]: ...


def agent_tool(
    function: ToolFunction | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: PermissionResolver | None = None,
) -> FunctionTool | Callable[[ToolFunction], FunctionTool]:
    """将函数声明为 Tool；支持 ``@agent_tool`` 和带参数形式。"""

    def decorate(target: ToolFunction) -> FunctionTool:
        return FunctionTool(
            target,
            name=name,
            description=description,
            permissions=permissions,
        )

    return decorate(function) if function is not None else decorate


def _build_arguments_model(function: ToolFunction) -> tuple[str | None, type[BaseModel]]:
    signature = inspect.signature(function)
    try:
        annotations = get_type_hints(function, include_extras=True)
    except (NameError, TypeError) as exc:
        raise TypeError(f"无法解析工具 {function.__name__} 的类型注解：{exc}") from exc

    context_parameter: str | None = None
    fields: dict[str, Any] = {}
    for parameter in signature.parameters.values():
        annotation = annotations.get(parameter.name, parameter.annotation)
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError(f"工具 {function.__name__} 不支持可变参数：{parameter.name}")
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise TypeError(f"工具 {function.__name__} 不支持位置专用参数：{parameter.name}")
        if parameter.name == "ctx":
            if annotation is not ToolContext:
                raise TypeError("保留参数 ctx 必须注解为 ToolContext")
            context_parameter = parameter.name
            continue
        if annotation is ToolContext:
            raise TypeError("ToolContext 注入参数必须命名为 ctx")
        if annotation is inspect.Parameter.empty:
            raise TypeError(f"工具 {function.__name__} 的参数 {parameter.name} 缺少类型注解")
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (annotation, default)

    model_name = f"{function.__name__.title().replace('_', '')}Arguments"
    model = create_model(
        model_name,
        __config__=ConfigDict(extra="ignore"),
        **fields,
    )
    return context_parameter, model


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "$"
    return f"{location}: {first['msg']}"


__all__ = ["FunctionTool", "PermissionResolver", "agent_tool"]
