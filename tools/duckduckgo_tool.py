"""DuckDuckGo web search tool — no API key required."""

from __future__ import annotations

from tools.base import BaseTool
from tools import registry


class DuckDuckGoTool(BaseTool):

    @property
    def name(self) -> str:
        return "duckduckgo"

    @property
    def display_name(self) -> str:
        return "🦆 DuckDuckGo"

    @property
    def description(self) -> str:
        return (
            "Search the web using DuckDuckGo (no API key needed). "
            "Use this for current events, news, real-time data, prices, weather, "
            "recent developments, or any question requiring up-to-date information."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def execute(self, query: str) -> str:
        from langchain_community.tools import DuckDuckGoSearchResults
        from langchain_community.utilities import DuckDuckGoSearchAPIWrapper

        wrapper = DuckDuckGoSearchAPIWrapper(max_results=8)
        search = DuckDuckGoSearchResults(
            api_wrapper=wrapper,
            output_format="list",
        )
        results = search.invoke(query)

        if not results:
            return f"No results found for: {query}"

        parts = []
        for i, r in enumerate(results, 1):
            snippet = r.get("snippet", "")
            title = r.get("title", "")
            link = r.get("link", "Unknown")
            parts.append(
                f"[Result {i}] {title}\n"
                f"{snippet}\n"
                f"SOURCE_URL: {link}"
            )
        return "\n\n---\n\n".join(parts)


registry.register(DuckDuckGoTool())
