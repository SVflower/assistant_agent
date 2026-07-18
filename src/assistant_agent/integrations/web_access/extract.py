"""无第三方解析依赖的有界 HTML 文本提取。"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SPACE = re.compile(r"[ \t\f\v]+")
_BLANKS = re.compile(r"\n{3,}")
_BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "p",
    "pre",
    "section",
    "table",
    "tr",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._title_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag in _BLOCK_TAGS and not self._skip_depth:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS and not self._skip_depth:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title_parts.append(data)
        if not self._skip_depth and not self._title_depth:
            self.text_parts.append(data)


def extract_html_text(html: str) -> tuple[str, str]:
    """返回 (title, readable_text)。解析坏 HTML 时尽力提取。"""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title = _clean_inline(" ".join(parser.title_parts))
    raw = "".join(parser.text_parts).replace("\r", "\n")
    lines = [_clean_inline(line) for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    return title, _BLANKS.sub("\n\n", text).strip()


def _clean_inline(value: str) -> str:
    return _SPACE.sub(" ", value).strip()
