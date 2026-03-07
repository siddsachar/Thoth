"""Conversation Search tool — search past conversations by keyword.

Exposes two sub-tools:
  • search_conversations — keyword search across all thread messages
  • list_conversations  — list all saved threads with names and dates
"""

from __future__ import annotations

import re
from datetime import datetime

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry


# ── Pydantic schemas ─────────────────────────────────────────────────────────

class _SearchConversationsInput(BaseModel):
    query: str = Field(
        description=(
            "Keywords or phrase to search for in past conversations. "
            "All words must appear in a message for it to match."
        )
    )
    max_results: int = Field(
        default=15,
        description="Maximum number of matching messages to return (default 15).",
    )


class _ListConversationsInput(BaseModel):
    pass  # no arguments needed


# ── Helpers ──────────────────────────────────────────────────────────────────

_SNIPPET_LEN = 500  # max chars per message snippet
_MAX_THREADS = 200  # safety cap on how many threads to scan


def _get_thread_messages(thread_id: str) -> list[dict]:
    """Load human + assistant messages from a thread's checkpoint.

    Uses the SqliteSaver checkpointer directly — no dependency on the
    agent graph or app.py.
    """
    from threads import checkpointer

    config = {"configurable": {"thread_id": thread_id}}
    try:
        checkpoint_tuple = checkpointer.get_tuple(config)
        if not checkpoint_tuple or not checkpoint_tuple.checkpoint:
            return []

        channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
        messages = channel_values.get("messages", [])

        result: list[dict] = []
        for m in messages:
            mtype = getattr(m, "type", None)
            content = getattr(m, "content", None)
            if not content or not isinstance(content, str):
                continue
            if mtype == "human":
                result.append({"role": "user", "content": content})
            elif mtype == "ai":
                result.append({"role": "assistant", "content": content})
        return result
    except Exception:
        return []


def _snippet(text: str, max_len: int = _SNIPPET_LEN) -> str:
    """Truncate text to *max_len* chars, adding ellipsis if needed."""
    return text[:max_len] + ("…" if len(text) > max_len else "")


# ── Tool functions ───────────────────────────────────────────────────────────

def _search_conversations(query: str, max_results: int = 15) -> str:
    """Search all past conversations for messages containing *query* keywords."""
    from threads import _list_threads

    threads = _list_threads()
    if not threads:
        return "No saved conversations found."

    # Split query into individual keywords for AND matching
    keywords = [kw.lower() for kw in query.split() if kw]
    if not keywords:
        return "Please provide at least one search keyword."

    matches: list[dict] = []
    threads_scanned = 0

    for tid, name, created_at, updated_at in threads[:_MAX_THREADS]:
        messages = _get_thread_messages(tid)
        threads_scanned += 1

        for msg in messages:
            content_lower = msg["content"].lower()
            if all(kw in content_lower for kw in keywords):
                matches.append({
                    "thread": name,
                    "thread_id": tid,
                    "date": (updated_at or "")[:10],
                    "role": msg["role"],
                    "snippet": _snippet(msg["content"]),
                })
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break

    if not matches:
        return (
            f"No messages matching '{query}' found "
            f"across {threads_scanned} conversation(s)."
        )

    parts: list[str] = []
    for i, m in enumerate(matches, 1):
        parts.append(
            f"[Match {i}] Thread: \"{m['thread']}\" | Date: {m['date']} | "
            f"{m['role'].title()}\n{m['snippet']}"
        )

    header = (
        f"Found {len(matches)} match(es) for '{query}' "
        f"across {threads_scanned} conversation(s):\n\n"
    )
    return header + "\n\n---\n\n".join(parts)


def _list_conversations() -> str:
    """List all saved conversations with names and dates."""
    from threads import _list_threads

    threads = _list_threads()
    if not threads:
        return "No saved conversations found."

    lines: list[str] = []
    for tid, name, created_at, updated_at in threads:
        date_str = (updated_at or created_at or "")[:16].replace("T", " ")
        lines.append(f"• \"{name}\" — last used: {date_str}")

    return f"{len(threads)} conversation(s):\n" + "\n".join(lines)


# ── Tool class ───────────────────────────────────────────────────────────────

class ConversationSearchTool(BaseTool):

    @property
    def name(self) -> str:
        return "conversation_search"

    @property
    def display_name(self) -> str:
        return "🔍 Conversation Search"

    @property
    def description(self) -> str:
        return (
            "Search past conversations by keyword to find what was "
            "discussed previously, or list all saved conversations."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_search_conversations,
                name="search_conversations",
                description=(
                    "Search past conversations by keyword. Use when the user "
                    "references something discussed previously, e.g. 'What did "
                    "I ask about taxes?', 'When did we talk about Python?', "
                    "'Find where I mentioned that recipe'. All keywords must "
                    "appear in a message for it to match."
                ),
                args_schema=_SearchConversationsInput,
            ),
            StructuredTool.from_function(
                func=_list_conversations,
                name="list_conversations",
                description=(
                    "List all saved conversations with names and dates. Use "
                    "when the user asks 'What conversations do I have?', "
                    "'Show me my chat history', or similar."
                ),
                args_schema=_ListConversationsInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _search_conversations(query)


registry.register(ConversationSearchTool())
