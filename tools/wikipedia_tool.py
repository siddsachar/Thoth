"""Wikipedia retrieval tool."""

from __future__ import annotations

from tools.base import BaseTool
from tools import registry


class WikipediaTool(BaseTool):

    @property
    def name(self) -> str:
        return "wikipedia"

    @property
    def display_name(self) -> str:
        return "🌐 Wikipedia"

    @property
    def description(self) -> str:
        return (
            "Search Wikipedia for encyclopedic knowledge, definitions, "
            "historical events, biographies, geography, science concepts, "
            "and other general-purpose factual information."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def get_retriever(self, **kwargs):
        from langchain_community.retrievers.wikipedia import WikipediaRetriever
        from agent import _compressed
        return _compressed(WikipediaRetriever())


registry.register(WikipediaTool())
