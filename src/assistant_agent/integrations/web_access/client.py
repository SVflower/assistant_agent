"""Web 搜索与抓取客户端。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx

from assistant_agent.execution import RunControl
from assistant_agent.integrations.web_access.extract import extract_html_text
from assistant_agent.integrations.web_access.security import (
    Resolver,
    URLPolicyError,
    system_resolver,
    validate_public_url,
)

if TYPE_CHECKING:
    from assistant_agent.config.schema import WebConfig
    from assistant_agent.integrations.web_access.backends import SearchBackend

_REDIRECTS = {301, 302, 303, 307, 308}
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")


class WebError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    content: str
    content_type: str
    fetched_at: str
    truncated: bool
    bytes_read: int


class WebClient:
    def __init__(
        self,
        config: WebConfig,
        *,
        http_client: httpx.Client | None = None,
        backend: SearchBackend | None = None,
        resolver: Resolver = system_resolver,
        run_control: RunControl | None = None,
    ) -> None:
        from assistant_agent.integrations.web_access.backends import build_search_backend

        self.config = config
        self._http = http_client or httpx.Client(timeout=config.request_timeout)
        self._owns_http = http_client is None
        self.backend = backend or build_search_backend(config, self._http)
        self._resolver = resolver
        self._run_control = run_control or RunControl()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def search(
        self, query: str, *, max_results: int | None = None, freshness: str | None = None
    ) -> tuple[list[SearchResult], str]:
        self._check_control()
        limit = min(max_results or self.config.search.max_results, self.config.search.max_results)
        try:
            rows = self.backend.search(query, limit, freshness)
        except httpx.TimeoutException as exc:
            raise WebError("Web 搜索超时", code="timeout", retryable=True) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in {408, 429} or exc.response.status_code >= 500
            raise WebError(
                f"Web 搜索返回 HTTP {exc.response.status_code}",
                code="http_error",
                retryable=retryable,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise WebError(f"Web 搜索失败：{exc}", code="search_error", retryable=True) from exc
        return rows[:limit], _now()

    def fetch(self, url: str) -> FetchedPage:
        self._check_control()
        current = url
        for redirect_count in range(self.config.max_redirects + 1):
            self._check_control()
            try:
                current = validate_public_url(current, self._resolver)
            except URLPolicyError as exc:
                raise WebError(str(exc), code="url_policy", retryable=False) from exc
            try:
                with self._http.stream(
                    "GET",
                    current,
                    headers={"User-Agent": "assistant-agent/0.1 (+local-web-fetch)"},
                ) as response:
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            raise WebError("重定向缺少 Location", code="bad_redirect")
                        if redirect_count >= self.config.max_redirects:
                            raise WebError("重定向次数超过上限", code="too_many_redirects")
                        current = urljoin(current, location)
                        continue
                    self._check_response(response)
                    data, truncated = self._read_limited(response)
                    content_type = response.headers.get("content-type", "text/plain").split(";", 1)[
                        0
                    ]
                    encoding = response.encoding or "utf-8"
                    text = data.decode(encoding, errors="replace")
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        title, content = extract_html_text(text)
                    else:
                        title, content = "", text.strip()
                    if len(content) > self.config.max_content_chars:
                        content = content[: self.config.max_content_chars]
                        truncated = True
                    return FetchedPage(
                        url=current,
                        title=title,
                        content=content,
                        content_type=content_type,
                        fetched_at=_now(),
                        truncated=truncated,
                        bytes_read=len(data),
                    )
            except WebError:
                raise
            except httpx.TimeoutException as exc:
                raise WebError("网页抓取超时", code="timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise WebError(
                    f"网页抓取失败：{exc}", code="network_error", retryable=True
                ) from exc
        raise WebError("重定向次数超过上限", code="too_many_redirects")

    def _check_response(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            raise WebError(
                f"网页返回 HTTP {response.status_code}", code="http_error", retryable=retryable
            )
        content_type = response.headers.get("content-type", "text/plain").lower()
        if not content_type.startswith(_TEXT_TYPES):
            raise WebError(f"不支持的内容类型：{content_type}", code="unsupported_content_type")
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > self.config.max_response_bytes:
            raise WebError("响应体超过大小上限", code="response_too_large")

    def _read_limited(self, response: httpx.Response) -> tuple[bytes, bool]:
        data = bytearray()
        for chunk in response.iter_bytes():
            self._check_control()
            remaining = self.config.max_response_bytes - len(data)
            if remaining <= 0:
                return bytes(data), True
            data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                return bytes(data), True
        return bytes(data), False

    def _check_control(self) -> None:
        if self._run_control.cancel_requested:
            raise WebError("任务已强制取消", code="cancelled", retryable=False)
        if self._run_control.pause_requested:
            raise WebError("任务已暂停", code="interrupted", retryable=False)

    @property
    def search_target(self) -> str:
        return self.backend.network_target


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hostname_for_url(url: str) -> str:
    return urlparse(url).hostname or "unknown"
