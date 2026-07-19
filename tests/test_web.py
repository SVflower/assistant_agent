"""M11b 结构化 Web 能力测试（全部使用本地 transport，不访问公网）。"""

from __future__ import annotations

import json

import httpx
import pytest

from assistant_agent.config.schema import WebConfig
from assistant_agent.execution import RunControl
from assistant_agent.integrations.web_access.backends import DuckDuckGoBackend, SearxngBackend
from assistant_agent.integrations.web_access.client import WebClient, WebError
from assistant_agent.integrations.web_access.extract import extract_html_text
from assistant_agent.integrations.web_access.security import URLPolicyError, validate_public_url
from assistant_agent.tools.permissions import Capability
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.web import FetchURLTool, WebSearchTool
from tests.support import ToolContextFixture

PUBLIC_IP = "93.184.216.34"


def _public_resolver(_host: str, _port: int) -> list[str]:
    return [PUBLIC_IP]


def _client(handler, config: WebConfig | None = None) -> tuple[httpx.Client, WebClient]:
    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=1)
    cfg = config or WebConfig()
    return http, WebClient(cfg, http_client=http, resolver=_public_resolver)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://user:pass@example.com/x",
        "http://10.0.0.1/x",
        "http://169.254.1.1/x",
    ],
)
def test_url_policy_rejects_unsafe_targets(url):
    with pytest.raises(URLPolicyError):
        validate_public_url(url, _public_resolver)


def test_url_policy_normalizes_public_url_and_drops_fragment():
    assert (
        validate_public_url("HTTPS://Example.COM/path?q=1#fragment", _public_resolver)
        == "https://example.com/path?q=1"
    )


def test_url_policy_rejects_dns_failure_and_private_dns_result():
    with pytest.raises(URLPolicyError, match="DNS"):
        validate_public_url("https://example.com", lambda *_args: [])
    with pytest.raises(URLPolicyError, match="非公网"):
        validate_public_url("https://example.com", lambda *_args: ["192.168.1.2"])


def test_extract_html_removes_scripts_and_preserves_blocks():
    title, text = extract_html_text(
        "<html><head><title> Example  Page </title><style>hidden</style></head>"
        "<body><main><h1>Hello</h1><p>First <b>paragraph</b>.</p>"
        "<script>alert(1)</script><p>Second</p></main></body></html>"
    )
    assert title == "Example Page"
    assert "Hello" in text and "First paragraph" in text and "Second" in text
    assert "hidden" not in text and "alert" not in text


def test_duckduckgo_backend_parses_results_and_unwraps_redirect():
    html = """
    <div class="result">
      <a class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">A result</a>
      <a class="result__snippet">A useful snippet</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.org/b">B result</a>
      <div class="result__snippet">B snippet</div>
    </div>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "html.duckduckgo.com"
        assert b"q=python" in request.content and b"df=w" in request.content
        return httpx.Response(200, text=html)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    rows = DuckDuckGoBackend(http).search("python", 2, "week")
    assert [(row.title, row.url, row.snippet) for row in rows] == [
        ("A result", "https://example.com/a", "A useful snippet"),
        ("B result", "https://example.org/b", "B snippet"),
    ]
    http.close()


def test_searxng_backend_parses_json_and_limits_results():
    payload = {
        "results": [
            {"title": "A", "url": "https://a.example", "content": "one"},
            {"title": "bad", "url": "javascript:bad", "content": "skip"},
            {"title": "B", "url": "https://b.example", "content": "two"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["time_range"] == "month"
        return httpx.Response(200, json=payload)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    rows = SearxngBackend(http, "https://search.example").search("q", 2, "month")
    assert [row.title for row in rows] == ["A", "B"]
    http.close()


def test_fetch_html_returns_source_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://example.com/page")
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<title>Docs</title><main><h1>Heading</h1><p>Body text</p></main>",
        )

    http, web = _client(handler)
    page = web.fetch("https://example.com/page#ignored")
    assert page.url == "https://example.com/page"
    assert page.title == "Docs"
    assert page.content == "Heading\nBody text"
    assert page.content_type == "text/html"
    assert page.truncated is False
    http.close()


def test_fetch_revalidates_redirect_and_blocks_private_destination():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    http, web = _client(handler)
    with pytest.raises(WebError) as caught:
        web.fetch("https://example.com/start")
    assert caught.value.code == "url_policy"
    assert calls == ["https://example.com/start"]
    http.close()


def test_fetch_rejects_binary_http_errors_and_declared_oversize():
    responses = {
        "/binary": httpx.Response(200, headers={"content-type": "image/png"}, content=b"x"),
        "/missing": httpx.Response(404, text="missing"),
        "/large": httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-length": "9000"},
            content=b"x",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return responses[request.url.path]

    http, web = _client(handler, WebConfig(max_response_bytes=1024))
    expected = {
        "/binary": "unsupported_content_type",
        "/missing": "http_error",
        "/large": "response_too_large",
    }
    for path, code in expected.items():
        with pytest.raises(WebError) as caught:
            web.fetch(f"https://example.com{path}")
        assert caught.value.code == code
    http.close()


def test_fetch_stream_and_content_limits_are_reported():
    body = ("a" * 1500).encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=httpx.ByteStream(body),
        )

    http, web = _client(handler, WebConfig(max_response_bytes=1024, max_content_chars=1000))
    page = web.fetch("https://example.com/large")
    assert page.bytes_read == 1024
    assert len(page.content) == 1000
    assert page.truncated is True
    http.close()


def test_web_tools_permissions_results_and_display():
    html = '<a class="result__a" href="https://example.com">Example</a>'
    html += '<a class="result__snippet">Snippet</a>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text=html)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="page")

    http, web = _client(handler)
    search = WebSearchTool(web)
    fetch = FetchURLTool(web)
    assert (
        search.permission_requests({"query": "q"}, ToolContextFixture())[0].capability
        == Capability.NETWORK_ACCESS
    )
    assert (
        fetch.permission_requests({"url": "https://example.com/x"}, ToolContextFixture())[0].target
        == "example.com"
    )

    registry = ToolRegistry()
    registry.register(search)
    registry.register(fetch)
    ctx = ToolContextFixture(interactive=True, confirm=lambda _message: "allow")
    result = registry.execute("web_search", {"query": "q"}, ctx)
    assert not result.is_error and result.metadata["result_count"] == 1
    assert result.metadata["source_urls"] == ["https://example.com"]
    assert registry.display_result("web_search", {"query": "q"}, result).summary == "找到 1 个来源"

    fetched = registry.execute("fetch_url", {"url": "https://example.com/x"}, ctx)
    assert not fetched.is_error and fetched.metadata["content_chars"] == 4
    assert "来源: https://example.com/x" in fetched.output
    assert json.dumps(result.metadata["results"], ensure_ascii=False)
    http.close()


def test_web_client_rejects_before_request_when_paused():
    control = RunControl()
    control.request_pause()
    client = WebClient(WebConfig(), run_control=control)
    try:
        with pytest.raises(WebError) as exc_info:
            client.fetch("https://example.com")
        assert exc_info.value.code == "interrupted"
    finally:
        client.close()
