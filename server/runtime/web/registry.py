"""Fixed dual-track: search=ddgs, extract=tavily."""

from __future__ import annotations

from server.runtime.web.provider import WebSearchProvider
from server.runtime.web.providers.ddgs import DDGSWebSearchProvider
from server.runtime.web.providers.tavily import TavilyWebSearchProvider

WEB_TOOL_NAMES = ("web_search", "web_extract")

_ddgs = DDGSWebSearchProvider()
_tavily = TavilyWebSearchProvider()


def get_search_provider() -> WebSearchProvider:
    return _ddgs


def get_extract_provider() -> WebSearchProvider:
    return _tavily
