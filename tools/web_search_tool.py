"""Web search retrieval tool (Tavily)."""

from __future__ import annotations

from tools.base import BaseTool
from tools import registry


class WebSearchTool(BaseTool):

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def display_name(self) -> str:
        return "🔍 Web Search"

    @property
    def description(self) -> str:
        return (
            "Search the live web for up-to-date information. "
            "Use this for current events, news, real-time data, prices, weather, "
            "product info, recent developments, or anything that may have changed "
            "since other knowledge sources were last updated."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {"Tavily API Key": "TAVILY_API_KEY"}

    def get_retriever(self, **kwargs):
        from langchain_community.retrievers.tavily_search_api import TavilySearchAPIRetriever
        from agent import _compressed
        return _compressed(TavilySearchAPIRetriever())


registry.register(WebSearchTool())
