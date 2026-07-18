"""结构化 Web 搜索与网页抓取基础设施。"""

from assistant_agent.integrations.web_access.backends import build_search_backend
from assistant_agent.integrations.web_access.client import (
    FetchedPage,
    SearchResult,
    WebClient,
    WebError,
)

__all__ = [
    "FetchedPage",
    "SearchResult",
    "WebClient",
    "WebError",
    "build_search_backend",
]
