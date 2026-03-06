"""Document retrieval tool — searches the local FAISS vector store."""

from __future__ import annotations

from tools.base import BaseTool
from tools import registry


class DocumentsTool(BaseTool):

    @property
    def name(self) -> str:
        return "documents"

    @property
    def display_name(self) -> str:
        return "📄 Documents"

    @property
    def description(self) -> str:
        return (
            "Search the user's personal uploaded documents and files. "
            "Use this tool whenever the user asks about their own notes, reports, "
            "manuals, or any content they have previously uploaded. "
            "Always check here first if the question could relate to the user's own materials."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def get_retriever(self, **kwargs):
        import documents
        from agent import _compressed
        retriever = documents.vector_store.as_retriever(search_kwargs={"k": 5})
        return _compressed(retriever)


registry.register(DocumentsTool())
