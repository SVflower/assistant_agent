"""受管文本输出的确定性发布前验证。"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from assistant_agent.contracts.outputs import OutputInvalidError

_VOID_HTML_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_MAX_CSV_COLUMNS = 512
_MAX_CSV_ROWS = 200_000


class OutputValidationError(OutputInvalidError):
    """不携带原始正文的稳定验证失败。"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class OutputValidationResult:
    media_type: str
    result_code: str


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.has_renderable_content = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if "head" not in self.stack and normalized in {"canvas", "img", "svg", "table", "video"}:
            self.has_renderable_content = True
        if normalized not in _VOID_HTML_ELEMENTS:
            self.stack.append(normalized)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1] == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if not self.stack or self.stack[-1] != normalized:
            raise OutputValidationError("html_structure_invalid", "HTML 标签未正确闭合")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if "head" not in self.stack and data.strip():
            self.has_renderable_content = True


def validate_output_content(media_type: str, content: str) -> OutputValidationResult:
    """验证模型生成的完整文本；不执行其中的脚本或外部资源。"""
    if not content.strip():
        raise OutputValidationError("output_empty", "输出正文为空")
    if "\x00" in content:
        raise OutputValidationError("output_control_character", "输出正文包含 NUL 字符")
    if media_type != "text/markdown" and _is_fenced_document(content):
        raise OutputValidationError("output_wrapped_in_code_fence", "文件正文被代码围栏包裹")

    validators = {
        "text/html": _validate_html,
        "application/json": _validate_json,
        "text/csv": _validate_csv,
        "text/markdown": _validate_markdown,
        "text/plain": _validate_plain_text,
    }
    validator = validators.get(media_type)
    if validator is None:
        raise OutputValidationError("output_media_type_unsupported", "没有对应的输出验证器")
    validator(content)
    return OutputValidationResult(media_type=media_type, result_code="output_validation_passed")


def _validate_html(content: str) -> None:
    parser = _DocumentParser()
    try:
        parser.feed(content)
        parser.close()
    except OutputValidationError:
        raise
    except Exception as exc:
        raise OutputValidationError("html_parse_failed", "HTML 无法解析") from exc
    if parser.stack:
        raise OutputValidationError("html_structure_invalid", "HTML 标签未完整闭合")
    if not parser.has_renderable_content:
        raise OutputValidationError("html_content_empty", "HTML 没有可展示内容")


def _validate_json(content: str) -> None:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        json.loads(content, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (TypeError, ValueError) as exc:
        raise OutputValidationError("json_invalid", "JSON 语法、数值或键唯一性无效") from exc


def _validate_csv(content: str) -> None:
    try:
        rows = csv.reader(io.StringIO(content, newline=""), strict=True)
        header = next(rows)
        if not header or any(not value.strip() for value in header):
            raise ValueError("empty header")
        if len(header) > _MAX_CSV_COLUMNS or len(set(header)) != len(header):
            raise ValueError("invalid header width or duplicate")
        row_count = 1
        for row in rows:
            row_count += 1
            if row_count > _MAX_CSV_ROWS or len(row) != len(header):
                raise ValueError("row limit or width mismatch")
    except (csv.Error, StopIteration, ValueError) as exc:
        raise OutputValidationError("csv_invalid", "CSV 表头、行宽或格式无效") from exc


def _validate_markdown(content: str) -> None:
    if not any(character.isalnum() for character in content):
        raise OutputValidationError("markdown_empty", "Markdown 没有有效文本内容")


def _validate_plain_text(content: str) -> None:
    if not content.strip():
        raise OutputValidationError("text_empty", "文本输出为空")


def _is_fenced_document(content: str) -> bool:
    stripped = content.strip()
    lines = stripped.splitlines()
    return len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```"


__all__ = ["OutputValidationError", "OutputValidationResult", "validate_output_content"]
