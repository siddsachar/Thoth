"""Arxiv retrieval tool."""

from __future__ import annotations

from langchain_core.documents import Document
from tools.base import BaseTool
from tools import registry


class ArxivTool(BaseTool):

    @property
    def name(self) -> str:
        return "arxiv"

    @property
    def display_name(self) -> str:
        return "📚 Arxiv"

    @property
    def description(self) -> str:
        return (
            "Search arXiv for academic and scientific research papers. "
            "Use this for any question about research, studies, scientific findings, "
            "machine learning, AI, physics, mathematics, computer science, "
            "or when the user asks for papers, citations, or scholarly references."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def get_retriever(self, **kwargs):
        from langchain_community.retrievers.arxiv import ArxivRetriever
        from agent import _compressed
        return _compressed(ArxivRetriever())

    def post_process(self, docs: list[Document]) -> list[Document]:
        """Rewrite source to use the Arxiv Entry ID when available."""
        for doc in docs:
            if "Entry ID" in doc.metadata:
                doc.metadata["source"] = doc.metadata["Entry ID"]
        return docs


registry.register(ArxivTool())
