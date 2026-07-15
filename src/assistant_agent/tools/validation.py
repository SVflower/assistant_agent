"""工具 JSON Schema 的注册期检查与执行期参数校验。"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class ToolSchemaError(ValueError):
    pass


def build_validator(tool_name: str, schema: dict[str, Any]) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolSchemaError(f"工具 {tool_name} 的参数 schema 无效：{exc.message}") from exc
    return Draft202012Validator(schema)


def validate_arguments(
    validator: Draft202012Validator, args: Any
) -> tuple[str, dict[str, Any]] | None:
    errors = sorted(validator.iter_errors(args), key=_error_sort_key)
    if not errors:
        return None
    first = errors[0]
    path = ".".join(str(part) for part in first.absolute_path) or "$"
    message = f"参数校验失败：{path}: {first.message}"
    return message, {
        "path": list(first.absolute_path),
        "validator": first.validator,
        "expected": first.validator_value,
        "error_count": len(errors),
    }


def _error_sort_key(error: ValidationError) -> tuple[str, str]:
    path = ".".join(str(part) for part in error.absolute_path)
    return path, error.message
