"""Web search — a keyless DuckDuckGo default + configurable third-party providers."""

from __future__ import annotations

from coworker.web.providers import (
    BraveProvider,
    DuckDuckGoProvider,
    SearchResult,
    TavilyProvider,
    WebSearchProvider,
    build_provider,
    provider_names,
)
from coworker.web.fetch import make_web_fetch_tool
from coworker.web.tool import make_web_search_tool, provider_host, provider_name, resolve_provider

__all__ = [
    "BraveProvider",
    "DuckDuckGoProvider",
    "SearchResult",
    "TavilyProvider",
    "WebSearchProvider",
    "build_provider",
    "make_web_fetch_tool",
    "make_web_search_tool",
    "provider_host",
    "provider_name",
    "provider_names",
    "resolve_provider",
]
