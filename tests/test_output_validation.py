from __future__ import annotations

import pytest

from assistant_agent.agent.output_validation import (
    OutputValidationError,
    validate_output_content,
)


@pytest.mark.parametrize(
    ("media_type", "content"),
    [
        ("text/html", "<!doctype html><html><body><main>Report</main></body></html>"),
        ("application/json", '{"items":[1,2],"ok":true}'),
        ("text/csv", 'name,value\n"Line, A",12\nLine B,13\n'),
        ("text/markdown", "# Report\n\nValidated content."),
        ("text/plain", "plain result"),
    ],
)
def test_supported_output_content_passes(media_type: str, content: str) -> None:
    result = validate_output_content(media_type, content)
    assert result.media_type == media_type
    assert result.result_code == "output_validation_passed"


@pytest.mark.parametrize(
    ("media_type", "content", "reason_code"),
    [
        ("text/html", "<html><body><div>broken</body></html>", "html_structure_invalid"),
        (
            "text/html",
            "<html><head><title>Only title</title></head><body></body></html>",
            "html_content_empty",
        ),
        ("application/json", '{"value":NaN}', "json_invalid"),
        ("application/json", '{"value":1,"value":2}', "json_invalid"),
        ("text/csv", "name,value\nA\n", "csv_invalid"),
        ("text/csv", "name,name\nA,B\n", "csv_invalid"),
        (
            "text/html",
            "```html\n<html><body>wrapped</body></html>\n```",
            "output_wrapped_in_code_fence",
        ),
        ("text/markdown", "---\n***\n", "markdown_empty"),
    ],
)
def test_invalid_output_fails_with_stable_reason(
    media_type: str, content: str, reason_code: str
) -> None:
    with pytest.raises(OutputValidationError) as raised:
        validate_output_content(media_type, content)
    assert raised.value.reason_code == reason_code
    assert content not in str(raised.value)
