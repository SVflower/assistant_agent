"""可替换的 Web 搜索 backend。"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

import httpx

if TYPE_CHECKING:
    from assistant_agent.config.schema import WebConfig
    from assistant_agent.web.client import SearchResult


class SearchBackend(Protocol):
    name: str
    network_target: str

    def search(self, query: str, max_results: int, freshness: str | None) -> list[SearchResult]: ...


class _DuckParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._capture: str | None = None
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._capture = "link"
            self._href = values.get("href") or ""
            self._parts = []
        elif "result__snippet" in classes:
            self._capture = "snippet"
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture == "link" and tag == "a":
            self.links.append((" ".join(self._parts).strip(), self._href))
            self._capture = None
        elif self._capture == "snippet" and tag in {"a", "div", "span"}:
            self.snippets.append(" ".join(self._parts).strip())
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data.strip())


class DuckDuckGoBackend:
    name = "duckduckgo"
    network_target = "html.duckduckgo.com"

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def search(self, query: str, max_results: int, freshness: str | None) -> list[SearchResult]:
        from assistant_agent.web.client import SearchResult

        data = {"q": query}
        if freshness:
            data["df"] = {"day": "d", "week": "w", "month": "m", "year": "y"}[freshness]
        response = self._client.post(
            "https://html.duckduckgo.com/html/",
            data=data,
            headers={"User-Agent": "assistant-agent/0.1 (+local-web-search)"},
        )
        response.raise_for_status()
        parser = _DuckParser()
        parser.feed(response.text)
        results: list[SearchResult] = []
        for index, (title, href) in enumerate(parser.links):
            url = _unwrap_duck_url(href)
            if not title or not url.startswith(("http://", "https://")):
                continue
            snippet = parser.snippets[index] if index < len(parser.snippets) else ""
            results.append(SearchResult(title=title, url=url, snippet=snippet, source=self.name))
            if len(results) >= max_results:
                break
        return results


class SearxngBackend:
    name = "searxng"

    def __init__(self, client: httpx.Client, endpoint: str) -> None:
        self._client = client
        self._endpoint = endpoint.rstrip("/")
        self.network_target = urlparse(endpoint).hostname or "searxng"

    def search(self, query: str, max_results: int, freshness: str | None) -> list[SearchResult]:
        from assistant_agent.web.client import SearchResult

        params: dict[str, Any] = {"q": query, "format": "json"}
        if freshness:
            params["time_range"] = freshness
        response = self._client.get(f"{self._endpoint}/search", params=params)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        out: list[SearchResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title, url = str(row.get("title", "")).strip(), str(row.get("url", "")).strip()
            if title and url.startswith(("http://", "https://")):
                out.append(
                    SearchResult(
                        title=title,
                        url=url,
                        snippet=str(row.get("content", "")).strip(),
                        source=self.name,
                    )
                )
            if len(out) >= max_results:
                break
        return out


def build_search_backend(config: WebConfig, client: httpx.Client) -> SearchBackend:
    if config.search.backend == "searxng":
        return SearxngBackend(client, config.search.searxng_url)
    return DuckDuckGoBackend(client)


def _unwrap_duck_url(value: str) -> str:
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return value
