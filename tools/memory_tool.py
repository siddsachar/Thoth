"""Memory tool — save, search, list, update, delete, link, and explore memories.

Exposes multiple LangChain sub-tools so the agent can manage a persistent
personal knowledge graph across conversations.  Categories: person, preference,
fact, event, place, project, organisation, concept, skill, media.

v3.6+: Two new sub-tools — ``link_memories`` (create relations between
entities) and ``explore_connections`` (traverse the knowledge graph).
"""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry
import memory as memory_db
import knowledge_graph as kg


# ── Pydantic schemas for structured input ────────────────────────────────────

class _SaveMemoryInput(BaseModel):
    category: str = Field(
        description=(
            "Memory category. Must be one of: person, preference, fact, "
            "event, place, project, organisation, concept, skill, media."
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
        description="Optional category filter (person, preference, fact, event, place, project, organisation, concept, skill, media). Leave empty to search all.",
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
        description="New content/description to replace the existing content."
    )
    subject: str | None = Field(
        default=None,
        description="New subject/name for the entity. Omit to keep unchanged.",
    )
    entity_type: str | None = Field(
        default=None,
        description="New entity type (person, preference, fact, event, place, project, organisation, concept, skill, media). Omit to keep unchanged.",
    )
    aliases: str | None = Field(
        default=None,
        description="Comma-separated aliases. Omit to keep unchanged.",
    )
    tags: str | None = Field(
        default=None,
        description="Comma-separated tags. Omit to keep unchanged.",
    )


class _DeleteMemoryInput(BaseModel):
    memory_id: str = Field(
        description="The ID of the memory to delete (from search or list output)."
    )


class _LinkMemoriesInput(BaseModel):
    source_id: str = Field(
        description=(
            "Source entity — pass the entity's **subject name** (e.g. 'Bob', "
            "'User', 'Atlas') or its hex ID. Names are preferred."
        )
    )
    target_id: str = Field(
        description=(
            "Target entity — pass the entity's **subject name** (e.g. 'User', "
            "'London', 'Dune') or its hex ID. Names are preferred."
        )
    )
    relation_type: str = Field(
        description=(
            "Label describing the relationship — e.g. 'father_of', 'lives_in', "
            "'works_on', 'friend_of', 'prefers', 'related_to'. Use snake_case."
        )
    )


class _ExploreConnectionsInput(BaseModel):
    entity_id: str = Field(
        description=(
            "Entity to explore — pass the **subject name** (e.g. 'Bob', "
            "'User', 'Atlas') or its hex ID. Names are preferred."
        )
    )
    hops: int = Field(
        default=1,
        description="Number of hops to traverse (1 = immediate neighbors, 2 = friends-of-friends).",
    )


# ── Contradiction detection ──────────────────────────────────────────────────

_CONTRADICTION_PROMPT = """\
You are checking whether two pieces of information about "{subject}" contradict each other.

Existing: {old_content}
New: {new_content}

Do these contain contradictory facts (e.g. different dates, numbers, names, \
or mutually exclusive statements about the same attribute)?

If YES, reply with a single short sentence describing the conflict.
If NO (they are compatible or additive), reply with exactly: NO"""


def _check_contradiction(old_content: str, new_content: str, subject: str) -> str | None:
    """Return a conflict description if old and new content contradict, else None."""
    try:
        from models import get_current_model, get_llm_for
        from langchain_core.messages import HumanMessage

        prompt = _CONTRADICTION_PROMPT.format(
            subject=subject,
            old_content=old_content,
            new_content=new_content,
        )
        llm = get_llm_for(get_current_model())
        resp = llm.invoke([HumanMessage(content=prompt)])
        raw = resp.content or ""
        if isinstance(raw, list):
            raw = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in raw
            )
        raw = raw.strip()
        if raw.upper().startswith("NO"):
            return None
        return raw
    except Exception:
        # On failure, allow the merge to proceed (no false blocks)
        return None


# ── Tool functions ───────────────────────────────────────────────────────────

def _save_memory(category: str, subject: str, content: str, tags: str = "") -> str:
    """Save a new memory, or update an existing one if a near-duplicate exists."""
    try:
        # Deterministic dedup: exact category + normalised subject match
        existing = memory_db.find_by_subject(category, subject)
        if existing:
            # Same subject already stored — merge content intelligently.
            old_content = existing.get("content", "").strip()
            new_content = content.strip()

            if new_content.lower() in old_content.lower():
                # New content is already captured — keep existing as-is
                merged = old_content
            elif old_content.lower() in new_content.lower():
                # New content is a superset — replace entirely
                merged = new_content
            else:
                # Both have unique info — check for contradiction before merging
                conflict = _check_contradiction(old_content, new_content, subject)
                if conflict:
                    return (
                        f"⚠️ CONFLICT for '{subject}': {conflict}\n"
                        f"Existing content: {old_content}\n"
                        f"New content: {new_content}\n"
                        f"Ask the user which is correct before updating."
                    )
                # No contradiction — safe to combine
                merged = f"{old_content}. {new_content}".replace(". . ", ". ")

            result = memory_db.update_memory(
                existing["id"],
                merged,
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
    except (RuntimeError, ValueError) as exc:
        return f"Error: {exc}"


def _search_memory(query: str, category: str = "") -> str:
    """Search memories using hybrid retrieval (FAISS + graph expansion + SQL LIKE)."""
    results = kg.graph_enhanced_recall(query, top_k=10, threshold=0.3)
    if category:
        cat = category.lower().strip()
        results = [m for m in results
                   if m.get("category", m.get("entity_type", "")) == cat]
    if not results:
        return "No memories found matching that query."
    entries = []
    for m in results:
        entry = {
            "id": m["id"],
            "category": m.get("category", m.get("entity_type", "")),
            "subject": m["subject"],
            "content": m.get("content", m.get("description", "")),
            "tags": m.get("tags", ""),
            "relevance": m.get("score", ""),
            "updated": m.get("updated_at", "")[:16],
        }
        # Include relationship context when available (from graph expansion)
        if m.get("via") == "graph" and m.get("relations"):
            rel_strs = [f"{r['from']} → {r['type']} → {r['to']}"
                        for r in m["relations"]]
            entry["connected_via"] = "; ".join(rel_strs)
        entries.append(entry)
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


def _update_memory(
    memory_id: str,
    content: str,
    subject: str | None = None,
    entity_type: str | None = None,
    aliases: str | None = None,
    tags: str | None = None,
) -> str:
    """Update an existing memory's content and metadata."""
    result = memory_db.update_memory(
        memory_id,
        content,
        subject=subject,
        category=entity_type,
        aliases=aliases,
        tags=tags,
    )
    if result is None:
        return f"Memory '{memory_id}' not found. Use search_memory or list_memories to find the correct ID."
    return (
        f"Memory updated successfully.\n"
        f"ID: {result['id']}\n"
        f"Subject: {result['subject']}\n"
        f"Type: {result.get('category', result.get('entity_type', ''))}\n"
        f"New content: {result['content']}"
    )


def _delete_memory(memory_id: str) -> str:
    """Delete a memory by ID."""
    if memory_db.delete_memory(memory_id):
        return f"Memory '{memory_id}' deleted."
    return f"Memory '{memory_id}' not found."


def _resolve_entity(name_or_id: str) -> dict | None:
    """Resolve an entity by subject name (preferred) or hex ID."""
    # Try by name first (most common path for the agent)
    entity = kg.find_by_subject(entity_type=None, subject=name_or_id)
    if entity:
        return entity
    # Fall back to direct ID lookup
    entity = kg.get_entity(name_or_id)
    if entity:
        return entity
    return None


def _link_memories(source_id: str, target_id: str, relation_type: str) -> str:
    """Create a relationship between two memories in the knowledge graph."""
    import time

    try:
        # Resolve by name or ID
        source_entity = _resolve_entity(source_id)
        target_entity = _resolve_entity(target_id)
        if not source_entity:
            # Retry once after a short delay — the entity may have been
            # created by a parallel tool call in the same batch.
            time.sleep(0.5)
            source_entity = _resolve_entity(source_id)
        if not source_entity:
            return f"Error: source entity '{source_id}' not found. Use search_memory or list_memories to find the correct name or ID."
        if not target_entity:
            # Retry once after a short delay — the entity may have been
            # created by a parallel tool call in the same batch.
            time.sleep(0.5)
            target_entity = _resolve_entity(target_id)
        if not target_entity:
            return f"Error: target entity '{target_id}' not found. Use search_memory or list_memories to find the correct name or ID."

        rel = kg.add_relation(source_entity['id'], target_entity['id'], relation_type)
        if rel:
            return (
                f"Relationship created successfully.\n"
                f"{source_entity['subject']} --[{rel['relation_type']}]--> {target_entity['subject']}\n"
                f"Relation ID: {rel['id']}"
            )
        return "Error: could not create relationship (entities may not exist)."
    except Exception as exc:
        return f"Error creating relationship: {exc}"


def _explore_connections(entity_id: str, hops: int = 1) -> str:
    """Explore the knowledge graph around an entity."""
    try:
        entity = _resolve_entity(entity_id)
        if not entity:
            return f"Entity '{entity_id}' not found. Use search_memory or list_memories to find the correct name or ID."

        resolved_id = entity['id']
        hops = max(1, min(hops, 3))  # cap at 3 to prevent huge traversals
        neighbors = kg.get_neighbors(resolved_id, hops=hops)
        relations = kg.get_relations(resolved_id)

        parts = [f"**{entity.get('subject', '?')}** ({entity.get('entity_type', '?')})"]

        if relations:
            parts.append(f"\nRelationships ({len(relations)}):")
            for rel in relations:
                arrow = "-->" if rel["direction"] == "outgoing" else "<--"
                parts.append(
                    f"  {arrow} [{rel['relation_type']}] {rel['peer_subject']} "
                    f"(id: {rel['peer_id']})"
                )

        if neighbors:
            graph_only = [n for n in neighbors if n not in [{"id": r["peer_id"]} for r in relations]]
            if len(neighbors) > len(relations):
                parts.append(f"\nNearby entities within {hops} hop(s): {len(neighbors)}")
                for n in neighbors[:15]:  # cap display
                    hop = n.get("hop", "?")
                    parts.append(
                        f"  [{hop} hop] {n.get('subject', '?')} ({n.get('entity_type', '?')}) "
                        f"(id: {n['id']})"
                    )
                if len(neighbors) > 15:
                    parts.append(f"  ... and {len(neighbors) - 15} more")

        if not relations and not neighbors:
            parts.append("\nNo connections found. Use link_memories to create relationships.")

        # Include Mermaid diagram for visual context
        mermaid = kg.to_mermaid(resolved_id, hops=hops, max_nodes=15)
        if mermaid and mermaid.count("\n") > 1:
            parts.append(f"\n```mermaid\n{mermaid}\n```")
            parts.append("\nInclude the mermaid diagram above verbatim in your response so the user can see the visual graph.")

        return "\n".join(parts)
    except Exception as exc:
        return f"Error exploring connections: {exc}"


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
            "facts, events, places, and projects. Connect memories with "
            "relationships to build a personal knowledge graph. Use this to "
            "remember personal details the user shares across conversations "
            "and explore how they relate to each other."
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
            StructuredTool.from_function(
                func=_link_memories,
                name="link_memories",
                description=(
                    "Create a relationship between two memories in the knowledge graph. "
                    "Use when the user mentions how things are related — e.g. 'Sarah is "
                    "my mom', 'I work at Acme Corp', 'The deadline is for Project X'. "
                    "Pass entity **subject names** (e.g. source_id='Bob', target_id='User') "
                    "or hex IDs. Names are preferred — no need to look up IDs first. "
                    "relation_type should be a snake_case label like 'mother_of', 'works_at', "
                    "'deadline_for'. Also use this proactively when you save related memories "
                    "to build connections."
                ),
                args_schema=_LinkMemoriesInput,
            ),
            StructuredTool.from_function(
                func=_explore_connections,
                name="explore_connections",
                description=(
                    "Explore the knowledge graph around a memory to see how it connects "
                    "to other memories. Shows relationships, nearby entities, and a Mermaid "
                    "graph diagram. Use when the user asks about how things are related, "
                    "'tell me about my family', 'what do you know about my work', etc."
                ),
                args_schema=_ExploreConnectionsInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return "Use save_memory, search_memory, list_memories, update_memory, delete_memory, link_memories, or explore_connections instead."


registry.register(MemoryTool())
