"""Memory tool — save, search, list, update, and delete long-term memories.

Exposes multiple LangChain sub-tools so the agent can manage a persistent
personal knowledge base across conversations.  Categories: person, preference,
fact, event, place, project.
"""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry
import memory as memory_db


# ── Pydantic schemas for structured input ────────────────────────────────────

class _SaveMemoryInput(BaseModel):
    category: str = Field(
        description=(
            "Memory category. Must be one of: person, preference, fact, "
            "event, place, project."
        )
    )
    subject: str = Field(
        description="Short identifier — a name, topic, or title (e.g. 'Mom', 'Coffee', 'Python 3.13')."
    )
    content: str = Field(
        description="Detailed information to remember (e.g. 'Mom's birthday is March 15', 'User prefers dark roast')."
    )
    tags: str = Field(
        default="",
        description="Optional comma-separated tags for easier search (e.g. 'family,birthday').",
    )


class _SearchMemoryInput(BaseModel):
    query: str = Field(
        description="Keyword or phrase to search for across subjects, content, and tags."
    )
    category: str = Field(
        default="",
        description="Optional category filter (person, preference, fact, event, place, project). Leave empty to search all.",
    )


class _ListMemoriesInput(BaseModel):
    category: str = Field(
        default="",
        description="Optional category filter. Leave empty to list all memories.",
    )


class _UpdateMemoryInput(BaseModel):
    memory_id: str = Field(
        description="The ID of the memory to update (from search or list output)."
    )
    content: str = Field(
        description="New content to replace the existing content."
    )


class _DeleteMemoryInput(BaseModel):
    memory_id: str = Field(
        description="The ID of the memory to delete (from search or list output)."
    )


# ── Tool functions ───────────────────────────────────────────────────────────

def _save_memory(category: str, subject: str, content: str, tags: str = "") -> str:
    """Save a new memory, or update an existing one if a near-duplicate exists."""
    try:
        # Deterministic dedup: exact category + normalised subject match
        existing = memory_db.find_by_subject(category, subject)
        if existing:
            # Same subject already stored — update with richer content
            new_content = content if len(content) >= len(existing.get("content", "")) else existing["content"]
            result = memory_db.update_memory(
                existing["id"],
                new_content,
                tags=tags if tags else None,
                source="live",
            )
            if result:
                return (
                    f"Memory updated (merged with existing).\n"
                    f"ID: {result['id']}\n"
                    f"Category: {result['category']}\n"
                    f"Subject: {result['subject']}\n"
                    f"Content: {result['content']}"
                )

        result = memory_db.save_memory(category, subject, content, tags, source="live")
        return (
            f"Memory saved successfully.\n"
            f"ID: {result['id']}\n"
            f"Category: {result['category']}\n"
            f"Subject: {result['subject']}\n"
            f"Content: {result['content']}"
        )
    except ValueError as exc:
        return f"Error: {exc}"


def _search_memory(query: str, category: str = "") -> str:
    """Search memories semantically."""
    results = memory_db.semantic_search(query, top_k=10, threshold=0.3)
    if category:
        cat = category.lower().strip()
        results = [m for m in results if m["category"] == cat]
    if not results:
        return "No memories found matching that query."
    entries = []
    for m in results:
        entries.append({
            "id": m["id"],
            "category": m["category"],
            "subject": m["subject"],
            "content": m["content"],
            "tags": m["tags"],
            "relevance": m.get("score", ""),
            "updated": m["updated_at"][:16],
        })
    return json.dumps(entries, indent=2)


def _list_memories(category: str = "") -> str:
    """List stored memories."""
    cat = category if category else None
    results = memory_db.list_memories(category=cat)
    if not results:
        return "No memories stored yet." if not cat else f"No memories in category '{cat}'."
    entries = []
    for m in results:
        entries.append({
            "id": m["id"],
            "category": m["category"],
            "subject": m["subject"],
            "content": m["content"],
            "tags": m["tags"],
            "updated": m["updated_at"][:16],
        })
    return json.dumps(entries, indent=2)


def _update_memory(memory_id: str, content: str) -> str:
    """Update an existing memory's content."""
    result = memory_db.update_memory(memory_id, content)
    if result is None:
        return f"Memory '{memory_id}' not found. Use search_memory or list_memories to find the correct ID."
    return (
        f"Memory updated successfully.\n"
        f"ID: {result['id']}\n"
        f"Subject: {result['subject']}\n"
        f"New content: {result['content']}"
    )


def _delete_memory(memory_id: str) -> str:
    """Delete a memory by ID."""
    if memory_db.delete_memory(memory_id):
        return f"Memory '{memory_id}' deleted."
    return f"Memory '{memory_id}' not found."


# ── Tool class ───────────────────────────────────────────────────────────────

class MemoryTool(BaseTool):

    @property
    def name(self) -> str:
        return "memory"

    @property
    def display_name(self) -> str:
        return "🧠 Memory"

    @property
    def description(self) -> str:
        return (
            "Save and recall long-term memories about people, preferences, "
            "facts, events, places, and projects. Use this to remember "
            "personal details the user shares across conversations."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    @property
    def destructive_tool_names(self) -> set[str]:
        return {"delete_memory"}

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_save_memory,
                name="save_memory",
                description=(
                    "Save a new long-term memory. Use when the user shares personal "
                    "information worth remembering: names, birthdays, relationships, "
                    "preferences, important facts, upcoming events, places, or projects. "
                    "Categories: person, preference, fact, event, place, project."
                ),
                args_schema=_SaveMemoryInput,
            ),
            StructuredTool.from_function(
                func=_search_memory,
                name="search_memory",
                description=(
                    "Search stored memories using semantic similarity. Use to "
                    "find specific memories about a person, topic, or preference. "
                    "Relevant memories are also auto-recalled each turn, but "
                    "this tool lets you do a deeper or more focused search."
                ),
                args_schema=_SearchMemoryInput,
            ),
            StructuredTool.from_function(
                func=_list_memories,
                name="list_memories",
                description=(
                    "List all stored memories, optionally filtered by category "
                    "(person, preference, fact, event, place, project). Use when "
                    "the user asks 'what do you remember' or wants to see all memories."
                ),
                args_schema=_ListMemoriesInput,
            ),
            StructuredTool.from_function(
                func=_update_memory,
                name="update_memory",
                description=(
                    "Update the content of an existing memory. Use when the user "
                    "corrects or adds to previously saved information. Requires "
                    "the memory ID (from search_memory or list_memories)."
                ),
                args_schema=_UpdateMemoryInput,
            ),
            StructuredTool.from_function(
                func=_delete_memory,
                name="delete_memory",
                description=(
                    "Delete a memory by its ID. Use when the user asks to forget "
                    "something. Requires the memory ID (from search_memory or "
                    "list_memories)."
                ),
                args_schema=_DeleteMemoryInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return "Use save_memory, search_memory, list_memories, update_memory, or delete_memory instead."


registry.register(MemoryTool())
