"""Base class for all Thoth tools."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from langchain_core.documents import Document
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


class BaseTool(ABC):
    """Every tool must subclass this and implement the required properties
    and the ``execute()`` method.  The tool registry discovers subclasses
    automatically.

    **Search tools** can override ``get_retriever()`` and optionally
    ``post_process()`` instead of ``execute()`` — the default ``execute()``
    handles retriever invocation, post-processing, and formatting.

    **Action tools** (non-retrieval) should override ``execute()`` directly.
    """

    # ── Identity ─────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def name(self) -> str:
        """Internal unique identifier (e.g. 'documents', 'wikipedia')."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable label shown in the Settings UI (e.g. '📄 Documents')."""
        ...

    @property
    def description(self) -> str:
        """One-line description shown as tooltip and passed to the agent."""
        return ""

    # ── Configuration ────────────────────────────────────────────────────────
    @property
    def required_api_keys(self) -> dict[str, str]:
        """Return ``{UI label: ENV_VAR_NAME}`` for any API keys this tool
        needs.  The Settings dialog will render input fields automatically.
        Return an empty dict if no keys are needed.
        """
        return {}

    @property
    def enabled_by_default(self) -> bool:
        """Whether the tool should be turned on for new installations."""
        return True

    # ── Execution (primary interface) ────────────────────────────────────────
    def execute(self, query: str) -> str:
        """Run the tool and return a text result.

        The default implementation calls ``get_retriever()`` →
        ``post_process()`` → format with sources.  Override this for
        non-retrieval tools (actions, calculations, etc.).
        """
        retriever = self.get_retriever()
        docs = retriever.invoke(query)
        docs = self.post_process(docs)
        if not docs:
            return f"No results found for: {query}"
        parts = []
        for i, doc in enumerate(docs, 1):
            source = doc.metadata.get("source", "Unknown")
            parts.append(
                f"[Result {i}]\n"
                f"{doc.page_content}\n"
                f"SOURCE_URL: {source}"
            )
        return "\n\n---\n\n".join(parts)

    # ── Retrieval (optional — used by default execute()) ─────────────────────
    def get_retriever(self, **kwargs) -> Any:
        """Return a LangChain retriever ready to ``.invoke(query)``.
        Only needed if using the default ``execute()`` implementation.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement either execute() or get_retriever()"
        )

    def post_process(self, docs: list[Document]) -> list[Document]:
        """Optional hook to fix-up metadata (e.g. rewrite ``source`` field)
        after retrieval.  Default implementation is a no-op passthrough.
        """
        return docs

    # ── Tool-specific configuration ───────────────────────────────────────────
    @property
    def config_schema(self) -> dict[str, dict]:
        """Declare tool-specific settings that should appear in the UI.

        Return a dict of ``{key: {"label": str, "type": "text"|"multicheck",
        "default": ..., "options": [...]}}``.  The registry persists values
        and the Settings dialog renders appropriate widgets.

        Override in subclasses that need custom configuration.
        """
        return {}

    def get_config(self, key: str, default=None):
        """Read a persisted config value for this tool."""
        from tools import registry
        return registry.get_tool_config(self.name, key, default)

    def set_config(self, key: str, value):
        """Write a config value for this tool."""
        from tools import registry
        registry.set_tool_config(self.name, key, value)

    # ── LangChain tool wrapper(s) for the agent ─────────────────────────────
    def as_langchain_tool(self) -> StructuredTool:
        """Return a single LangChain ``StructuredTool`` wrapping ``execute()``."""
        tool_instance = self

        def _run(query: str) -> str:
            try:
                return tool_instance.execute(query)
            except Exception as exc:
                logger.error("Tool '%s' execute error: %s", tool_instance.name, exc, exc_info=True)
                return f"Error in {tool_instance.display_name}: {exc}"

        return StructuredTool.from_function(
            func=_run,
            name=self.name,
            description=f"{self.display_name}: {self.description}",
        )

    def as_langchain_tools(self) -> list:
        """Return one or more LangChain tools for the agent.

        The default returns ``[self.as_langchain_tool()]``.  Override for
        tools that contribute multiple operations (e.g. filesystem).
        """
        return [self.as_langchain_tool()]

    @property
    def destructive_tool_names(self) -> set[str]:
        """Names of sub-tools that require explicit user confirmation
        before execution (e.g. delete_file, delete_calendar_event).

        Override in subclasses to gate high-impact operations with a
        LangGraph ``interrupt()`` so the user can approve or cancel.
        """
        return set()
