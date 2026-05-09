"""Personal Knowledge Graph — entity-relation graph with SQLite + NetworkX.

Replaces the flat ``memories`` table with a connected graph of **entities**
(people, places, facts, preferences, events, projects, …) and **relations**
(edges like ``father_of``, ``lives_in``, ``works_on``).

Architecture
~~~~~~~~~~~~
* **SQLite** is the durable store (WAL mode, same ``~/.thoth/memory.db``).
* **NetworkX** ``MultiDiGraph`` is an in-memory mirror rebuilt on startup from
  SQLite.  All reads hit the graph; all writes go to SQLite first, then
  update NetworkX and the FAISS index atomically.
* **FAISS** vector index is preserved for semantic recall — embeddings are
  built from each entity's combined text (type + subject + description +
  aliases + properties).

Migration
~~~~~~~~~
On first import the module checks for a legacy ``memories`` table and
migrates every row into an ``entities`` row, preserving IDs, timestamps,
and all content.  The old table is renamed to ``memories_v35_backup`` so
data is never lost.

Public API is consumed by ``memory.py`` (thin backward-compatible wrapper),
``tools/memory_tool.py``, ``memory_extraction.py``, and ``agent.py``.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

import threading

import networkx as nx
import numpy as np

logger = logging.getLogger(__name__)

# Lock protecting FAISS index reads/writes — FAISS is not thread-safe
# and concurrent access from agent + extraction threads causes segfaults.
_faiss_lock = threading.Lock()

# Lock protecting the in-memory NetworkX graph.  Multiple threads
# (live saves, extraction timer, dream daemon) mutate the graph
# concurrently.  RLock is used because nested calls exist (e.g.
# _dedup_and_save → save_memory → save_entity → add_relation).
_graph_lock = threading.RLock()

# When True, save_entity / update_entity / delete_entity skip the
# per-call rebuild_index().  Callers must call rebuild_index() once
# after the batch.  Used by the extraction pipeline.
_skip_reindex = False

# PDF extraction and web scraping can introduce lone UTF-16 surrogates
# (U+D800–U+DFFF) into text.  These are valid in Python str but
# invalid in strict UTF-8, causing orjson (NiceGUI) to crash on encode.
_SURROGATE_RE = re.compile('[\ud800-\udfff]')


def _sanitize_text(s: str) -> str:
    """Strip lone UTF-16 surrogates that are invalid in strict UTF-8."""
    return _SURROGATE_RE.sub('', s) if s else s


def extract_json_block(text: str, bracket: str = "[") -> str | None:
    """Extract the first balanced JSON array or object from *text*.

    Uses bracket-counting instead of greedy regex so nested structures
    (e.g. ``[[1,2],[3,4]]``) are matched correctly and stray brackets
    in surrounding prose are ignored.

    Parameters
    ----------
    text : str
        Raw LLM response that may contain a JSON block.
    bracket : str
        ``'['`` to find an array, ``'{'`` to find an object.

    Returns the matched substring (valid for ``json.loads``) or ``None``.
    """
    open_br = bracket
    close_br = "]" if bracket == "[" else "}"
    start = text.find(open_br)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue

        if ch == "\\":
            if in_string:
                escape_next = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == open_br:
            depth += 1
        elif ch == close_br:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None  # unbalanced brackets

# ── Data directory ───────────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(_DATA_DIR / "memory.db")
_VECTOR_DIR = _DATA_DIR / "memory_vectors"

# Entity types — superset of the old memory categories.  Open string: the
# LLM can use any of these, but we guide it toward the canonical set.
VALID_ENTITY_TYPES = {
    "person", "preference", "fact", "event", "place", "project",
    "organisation", "concept", "skill", "media", "self_knowledge",
}

# Keep backward compat alias
VALID_CATEGORIES = VALID_ENTITY_TYPES

# Controlled vocabulary of allowed relation types.
# Any relation created with a type NOT in this set will be logged as a
# warning (but still accepted to avoid breaking existing data).
# Dream inference uses this to reject vague types.
VALID_RELATION_TYPES = {
    # Family / social
    "knows", "friend_of", "colleague_of", "boss_of", "mentor_of",
    "mother_of", "father_of", "sibling_of", "married_to", "child_of",
    "partner_of", "parent_of", "family_member_of", "cousin_of",
    # Location
    "lives_in", "works_at", "located_in", "born_in", "visits",
    "based_in", "headquarters_in",
    # Work / organisational
    "works_on", "manages", "member_of", "part_of", "employed_by",
    "founded", "leads", "reports_to", "manager_of",
    # Preference / interest
    "prefers", "enjoys", "dislikes", "interested_in", "has_hobby",
    # Temporal
    "deadline_for", "scheduled_for", "started_on", "completed_on",
    # Knowledge / skill
    "studies", "proficient_in", "certified_in", "learning",
    "has_skill", "teaches",
    # Media
    "reading", "watching", "recommends", "authored", "listening_to",
    # General / ownership
    "uses", "created_by", "owns", "has_pet", "pet_of",
    "treats", "attends", "participates_in",
    # Auto-link system types
    "related_to", "associated_with", "has_event",
    # Document extraction types
    "extracted_from", "uploaded",
    "builds_on", "cites", "extends", "contradicts",
}

# Aliases map common LLM-produced variants to their canonical form.
# Checked *before* the VALID_RELATION_TYPES warning so normalised types
# never produce a warning.
_RELATION_ALIASES: dict[str, str] = {
    # is_X_of → X_of
    "is_father_of": "father_of",
    "is_mother_of": "mother_of",
    "is_sibling_of": "sibling_of",
    "is_child_of": "child_of",
    "is_parent_of": "parent_of",
    "is_friend_of": "friend_of",
    "is_colleague_of": "colleague_of",
    "is_boss_of": "boss_of",
    "is_mentor_of": "mentor_of",
    "is_member_of": "member_of",
    "is_part_of": "part_of",
    "is_pet_of": "pet_of",
    # Synonym mapping
    "works_for": "employed_by",
    "employed_at": "employed_by",
    "resides_in": "lives_in",
    "living_in": "lives_in",
    "likes": "enjoys",
    "hates": "dislikes",
    "wrote": "authored",
    "written_by": "authored",
    "reads": "reading",
    "watches": "watching",
    "listens_to": "listening_to",
    "skilled_in": "proficient_in",
    "expert_in": "proficient_in",
    "located_at": "located_in",
    "situated_in": "located_in",
    "managed_by": "reports_to",
    "supervised_by": "reports_to",
    "supervises": "manages",
    "head_of": "leads",
    "leading": "leads",
    "belongs_to": "part_of",
    "affiliated_with": "member_of",
    "lover_of": "partner_of",
    "spouse_of": "married_to",
    "husband_of": "married_to",
    "wife_of": "married_to",
    "visited": "visits",
    "visiting": "visits",
    "founded_by": "founded",
    "created": "created_by",
    "made_by": "created_by",
    "participates": "participates_in",
    "attending": "attends",
    "attended": "attends",
    "studying": "studies",
    "studied": "studies",
    "teaching": "teaches",
    "taught": "teaches",
    "owns_pet": "has_pet",
    "interested": "interested_in",
    "hobby": "has_hobby",
    "has_interest": "interested_in",
    # Document extraction types
    "extracted_from": "extracted_from",
    "uploaded": "uploaded",
    "published_by": "authored",
    "implements": "uses",
    "used_by": "uses",
    "references": "cites",
}


def normalize_relation_type(relation_type: str) -> str:
    """Map *relation_type* to its canonical form.

    1. Exact match in ``_RELATION_ALIASES`` → return mapped value.
    2. Strip ``is_`` or ``has_`` prefix and check ``VALID_RELATION_TYPES``.
    3. Otherwise return the original (unchanged).
    """
    rt = relation_type.lower().strip().replace(" ", "_")
    # 1. Explicit alias
    if rt in _RELATION_ALIASES:
        return _RELATION_ALIASES[rt]
    # Already valid — no change needed
    if rt in VALID_RELATION_TYPES:
        return rt
    # 2. Strip is_ / has_ prefix
    for prefix in ("is_", "has_"):
        if rt.startswith(prefix):
            stripped = rt[len(prefix):]
            if stripped in VALID_RELATION_TYPES:
                return stripped
    return rt





# ═════════════════════════════════════════════════════════════════════════════
# Wiki vault hooks (fire-and-forget — never block graph operations)
# ═════════════════════════════════════════════════════════════════════════════

def _wiki_export_entity(entity: dict) -> None:
    """Export an entity to the wiki vault (if enabled).  Non-blocking.

    Skipped when ``_skip_reindex`` is True (batch extraction) — a single
    ``rebuild_vault()`` is called at the end of the extraction run instead.
    """
    if _skip_reindex:
        return
    try:
        import wiki_vault
        if wiki_vault.is_enabled():
            wiki_vault.export_entity(entity)
    except Exception as exc:
        logger.debug("Wiki export skipped: %s", exc)


def _wiki_delete_entity(entity: dict) -> None:
    """Remove an entity's .md file from the wiki vault (if enabled)."""
    try:
        import wiki_vault
        if wiki_vault.is_enabled():
            wiki_vault.delete_entity_md(entity)
    except Exception as exc:
        logger.debug("Wiki delete skipped: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
# SQLite schema & connection
# ═════════════════════════════════════════════════════════════════════════════

def _get_conn() -> sqlite3.Connection:
    """Return a connection with WAL mode and row-factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db() -> None:
    """Create entities + relations tables (idempotent)."""
    conn = _get_conn()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id            TEXT PRIMARY KEY,
            entity_type   TEXT NOT NULL,
            subject       TEXT NOT NULL,
            description   TEXT NOT NULL DEFAULT '',
            aliases       TEXT NOT NULL DEFAULT '',
            tags          TEXT NOT NULL DEFAULT '',
            properties    TEXT NOT NULL DEFAULT '{}',
            source        TEXT NOT NULL DEFAULT 'live',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entities_subject ON entities(subject)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS relations (
            id              TEXT PRIMARY KEY,
            source_id       TEXT NOT NULL,
            target_id       TEXT NOT NULL,
            relation_type   TEXT NOT NULL,
            confidence      REAL NOT NULL DEFAULT 1.0,
            properties      TEXT NOT NULL DEFAULT '{}',
            source          TEXT NOT NULL DEFAULT 'live',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(relation_type)"
    )
    # Prevent exact duplicate edges
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_unique
        ON relations(source_id, target_id, relation_type)
    """)

    conn.commit()
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Migration from legacy memories table
# ═════════════════════════════════════════════════════════════════════════════

def _migrate_from_memories() -> int:
    """Migrate rows from the legacy ``memories`` table into ``entities``.

    Preserves original IDs, timestamps, content, and all metadata.  The
    old table is renamed to ``memories_v35_backup`` so data is never lost.

    Returns the number of rows migrated.
    """
    conn = _get_conn()

    # Check if legacy table exists
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "memories" not in tables:
        conn.close()
        return 0

    # Check if already migrated (backup table exists)
    if "memories_v35_backup" in tables:
        conn.close()
        return 0

    logger.info("Migrating legacy memories table to knowledge graph entities…")

    rows = conn.execute("SELECT * FROM memories").fetchall()
    migrated = 0

    for row in rows:
        row = dict(row)
        # Map old columns to new schema
        entity_id = row["id"]
        entity_type = row.get("category", "fact").lower().strip()
        subject = row.get("subject", "").strip()
        description = row.get("content", "").strip()
        tags = row.get("tags", "").strip()
        source = row.get("source", "live").strip()
        created_at = row.get("created_at", datetime.now().isoformat())
        updated_at = row.get("updated_at", created_at)

        # Check for collisions (shouldn't happen, but be safe)
        existing = conn.execute(
            "SELECT id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if existing:
            continue

        conn.execute(
            "INSERT INTO entities "
            "(id, entity_type, subject, description, aliases, tags, properties, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_id, entity_type, subject, description, "", tags, "{}", source, created_at, updated_at),
        )
        migrated += 1

    # Rename old table as backup
    conn.execute("ALTER TABLE memories RENAME TO memories_v35_backup")
    conn.commit()
    conn.close()

    logger.info("Migrated %d memories → entities. Backup at 'memories_v35_backup'.", migrated)
    return migrated


# ═════════════════════════════════════════════════════════════════════════════
# Initialise on import
# ═════════════════════════════════════════════════════════════════════════════

_init_db()
_migrate_from_memories()


def _scrub_surrogates() -> None:
    """One-time startup cleanup: strip surrogate chars from existing entities.

    PDF text extraction can inject lone UTF-16 surrogates into entity text.
    These are valid Python str but invalid in strict UTF-8, crashing orjson
    when NiceGUI serialises graph data for the browser.  This scan fixes
    existing rows so the data layer is clean going forward.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, subject, description, aliases, tags FROM entities"
    ).fetchall()
    updates = []
    for row in rows:
        r = dict(row)
        cleaned = {}
        dirty = False
        for col in ("subject", "description", "aliases", "tags"):
            val = r[col] or ""
            scrubbed = _sanitize_text(val)
            if scrubbed != val:
                dirty = True
            cleaned[col] = scrubbed
        if dirty:
            updates.append((
                cleaned["subject"], cleaned["description"],
                cleaned["aliases"], cleaned["tags"], r["id"],
            ))
    if updates:
        conn.executemany(
            "UPDATE entities SET subject=?, description=?, aliases=?, tags=? "
            "WHERE id=?",
            updates,
        )
        conn.commit()
        logger.info(
            "Scrubbed surrogate characters from %d entities.", len(updates)
        )
    conn.close()


_scrub_surrogates()


# ═════════════════════════════════════════════════════════════════════════════
# NetworkX in-memory graph
# ═════════════════════════════════════════════════════════════════════════════

_graph: nx.MultiDiGraph = nx.MultiDiGraph()
_graph_ready = False


def _load_graph() -> None:
    """Populate the NetworkX graph from SQLite.  Called once at startup."""
    global _graph, _graph_ready
    with _graph_lock:
        _graph = nx.MultiDiGraph()
        conn = _get_conn()

        # Load entities as nodes
        for row in conn.execute("SELECT * FROM entities").fetchall():
            row = dict(row)
            _graph.add_node(row["id"], **row)

        # Load relations as edges (use relation id as edge key)
        for row in conn.execute("SELECT * FROM relations").fetchall():
            row = dict(row)
            if row["source_id"] in _graph and row["target_id"] in _graph:
                _graph.add_edge(
                    row["source_id"],
                    row["target_id"],
                    key=row["id"],
                    id=row["id"],
                    relation_type=row["relation_type"],
                    confidence=row["confidence"],
                    properties=row["properties"],
                    source=row["source"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )

        _graph_ready = True
        logger.info(
            "Knowledge graph loaded: %d entities, %d relations",
            _graph.number_of_nodes(),
            _graph.number_of_edges(),
        )
        conn.close()


def _ensure_graph() -> nx.MultiDiGraph:
    """Return the graph, loading from SQLite if needed."""
    global _graph_ready
    if not _graph_ready:
        _load_graph()
    return _graph


# ═════════════════════════════════════════════════════════════════════════════
# FAISS vector index (shared with documents.py embedding model)
# ═════════════════════════════════════════════════════════════════════════════

def _get_embedding_model():
    """Return the shared HuggingFaceEmbeddings instance from documents.py."""
    from documents import get_embedding_model
    return get_embedding_model()


def _entity_text(entity: dict) -> str:
    """Build the string that gets embedded for an entity."""
    parts = [
        entity.get("entity_type", ""),
        entity.get("subject", ""),
        entity.get("description", ""),
    ]
    aliases = entity.get("aliases", "")
    if aliases:
        parts.append(aliases)
    tags = entity.get("tags", "")
    if tags:
        parts.append(tags)
    # Include key properties in embedding
    props = entity.get("properties", "{}")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (json.JSONDecodeError, TypeError):
            props = {}
    if props:
        parts.append(" ".join(f"{k}:{v}" for k, v in props.items()))
    return " | ".join(p for p in parts if p)


def rebuild_index() -> None:
    """(Re)build the FAISS index from all entities in SQLite."""
    import faiss as _faiss
    from embedding_config import write_index_metadata
    from stability import log_performance_snapshot

    started = time.perf_counter()
    entities = list_entities(limit=100_000)
    _VECTOR_DIR.mkdir(parents=True, exist_ok=True)

    if not entities:
        emb = _get_embedding_model()
        dim = len(emb.embed_query("test"))
        index = _faiss.IndexFlatIP(dim)
        with _faiss_lock:
            _faiss.write_index(index, str(_VECTOR_DIR / "index.faiss"))
            (_VECTOR_DIR / "id_map.json").write_text("[]")
            write_index_metadata(_VECTOR_DIR)
        return

    emb = _get_embedding_model()
    texts = [_entity_text(e) for e in entities]
    vectors = emb.embed_documents(texts)
    arr = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1
    arr = arr / norms

    dim = arr.shape[1]
    index = _faiss.IndexFlatIP(dim)
    index.add(arr)

    with _faiss_lock:
        _faiss.write_index(index, str(_VECTOR_DIR / "index.faiss"))
        id_map = [e["id"] for e in entities]
        (_VECTOR_DIR / "id_map.json").write_text(json.dumps(id_map))
        write_index_metadata(_VECTOR_DIR)
    logger.info(
        "Rebuilt FAISS index with %d entities in %.3fs",
        len(id_map),
        time.perf_counter() - started,
    )
    log_performance_snapshot("memory-faiss-rebuild")


def _upsert_index(entity_id: str) -> None:
    """Add or update a single entity in the FAISS index incrementally.

    Much faster than ``rebuild_index()`` because it only embeds one text
    and appends/replaces one vector.  Stale duplicate entries (from
    updates) are cleaned up on the next ``rebuild_index()`` call.
    """
    import faiss as _faiss

    _VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    index_path = _VECTOR_DIR / "index.faiss"
    map_path = _VECTOR_DIR / "id_map.json"
    from embedding_config import index_metadata_matches, write_index_metadata

    entity = get_entity(entity_id)
    if not entity:
        return

    emb = _get_embedding_model()
    text = _entity_text(entity)
    vec = np.array(emb.embed_query(text), dtype=np.float32).reshape(1, -1)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm

    with _faiss_lock:
        # Load existing index, or create empty one
        if index_path.exists() and map_path.exists() and index_metadata_matches(_VECTOR_DIR):
            index = _faiss.read_index(str(index_path))
            id_map: list[str] = json.loads(map_path.read_text())
        else:
            if index_path.exists() and not index_metadata_matches(_VECTOR_DIR):
                logger.warning("Memory FAISS index is stale for active embedding model; starting compatible index")
            dim = vec.shape[1]
            index = _faiss.IndexFlatIP(dim)
            id_map = []

        # If entity already in id_map (update), remove old entry
        if entity_id in id_map:
            old_idx = id_map.index(entity_id)
            # Rebuild without the old vector — IndexFlatIP doesn't
            # support removal, so reconstruct from remaining vectors
            n = index.ntotal
            if n > 1:
                all_vecs = np.vstack([index.reconstruct(i) for i in range(n) if i != old_idx])
                new_map = [eid for i, eid in enumerate(id_map) if i != old_idx]
                dim = all_vecs.shape[1]
                index = _faiss.IndexFlatIP(dim)
                index.add(all_vecs)
                id_map = new_map
            else:
                dim = vec.shape[1]
                index = _faiss.IndexFlatIP(dim)
                id_map = []

        # Append new vector
        index.add(vec)
        id_map.append(entity_id)

        _faiss.write_index(index, str(index_path))
        map_path.write_text(json.dumps(id_map))
        write_index_metadata(_VECTOR_DIR)


def _remove_from_index(entity_id: str) -> None:
    """Remove a single entity from the FAISS index without full rebuild.

    Much faster than ``rebuild_index()`` because it only reconstructs
    existing vectors — no embedding calls needed.
    """
    import faiss as _faiss

    index_path = _VECTOR_DIR / "index.faiss"
    map_path = _VECTOR_DIR / "id_map.json"

    with _faiss_lock:
        if not index_path.exists() or not map_path.exists():
            return
        index = _faiss.read_index(str(index_path))
        id_map: list[str] = json.loads(map_path.read_text())
        if entity_id not in id_map:
            return

        old_idx = id_map.index(entity_id)
        n = index.ntotal
        if n > 1:
            all_vecs = np.vstack(
                [index.reconstruct(i) for i in range(n) if i != old_idx]
            )
            new_map = [eid for i, eid in enumerate(id_map) if i != old_idx]
            dim = all_vecs.shape[1]
            index = _faiss.IndexFlatIP(dim)
            index.add(all_vecs)
            id_map = new_map
        else:
            dim = index.d
            index = _faiss.IndexFlatIP(dim)
            id_map = []

        _faiss.write_index(index, str(index_path))
        map_path.write_text(json.dumps(id_map))


# ═════════════════════════════════════════════════════════════════════════════
# Entity CRUD
# ═════════════════════════════════════════════════════════════════════════════

def _normalize_subject(s: str) -> str:
    """Lower-case, strip, collapse whitespace — for subject comparison."""
    return " ".join(s.lower().split())


def save_entity(
    entity_type: str,
    subject: str,
    description: str = "",
    *,
    aliases: str = "",
    tags: str = "",
    properties: dict | None = None,
    source: str = "live",
) -> dict:
    """Create a new entity in the knowledge graph.

    Parameters
    ----------
    entity_type : str
        Category / type (e.g. person, fact, preference).
    subject : str
        Short identifier — a name, topic, or title.
    description : str
        Free-text detail about the entity.
    aliases : str
        Comma-separated alternative names for entity resolution
        (e.g. "Mom, Mother, Mama").
    tags : str
        Comma-separated tags for search.
    properties : dict, optional
        Structured metadata as JSON-serialisable dict
        (e.g. {"birthday": "1965-03-15", "phone": "+1-555-0199"}).
    source : str
        Origin: 'live' or 'extraction'.

    Returns
    -------
    dict  with all entity columns.
    """
    entity_type = entity_type.lower().strip()
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(
            f"Invalid entity type '{entity_type}'. "
            f"Must be one of: {', '.join(sorted(VALID_ENTITY_TYPES))}"
        )

    # Prevent duplicate User entities — redirect to update if one exists
    if _normalize_subject(subject) == "user":
        existing_user = find_by_subject(None, "User")
        if existing_user:
            # Merge description if new content adds info
            old_desc = existing_user.get("description", "") or ""
            new_desc = description.strip()
            if new_desc and new_desc.lower() not in old_desc.lower():
                merged = f"{old_desc}. {new_desc}".strip(". ") if old_desc else new_desc
            else:
                merged = old_desc
            updated = update_entity(
                existing_user["id"],
                merged or old_desc,
                entity_type="person",
            )
            return updated if updated else existing_user

    entity_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    props_json = json.dumps(properties or {})

    conn = _get_conn()
    _subject = _sanitize_text(subject.strip())
    _description = _sanitize_text(description.strip())
    _aliases = _sanitize_text(aliases.strip())
    _tags = _sanitize_text(tags.strip())

    conn.execute(
        "INSERT INTO entities "
        "(id, entity_type, subject, description, aliases, tags, properties, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, entity_type, _subject, _description,
         _aliases, _tags, props_json, source.strip(), now, now),
    )
    conn.commit()
    conn.close()

    entity = {
        "id": entity_id,
        "entity_type": entity_type,
        "subject": _subject,
        "description": _description,
        "aliases": _aliases,
        "tags": _tags,
        "properties": props_json,
        "source": source.strip(),
        "created_at": now,
        "updated_at": now,
    }

    # Update NetworkX
    with _graph_lock:
        g = _ensure_graph()
        g.add_node(entity_id, **entity)

    # Update FAISS (skipped during batch extraction)
    if not _skip_reindex:
        _upsert_index(entity_id)

    # Wiki vault export (non-blocking, fire-and-forget)
    _wiki_export_entity(entity)

    return entity


def get_entity(entity_id: str) -> dict | None:
    """Fetch a single entity by ID."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_entity(
    entity_id: str,
    description: str,
    *,
    subject: str | None = None,
    entity_type: str | None = None,
    aliases: str | None = None,
    tags: str | None = None,
    properties: dict | None = None,
    source: str | None = None,
) -> dict | None:
    """Update an existing entity's fields.

    Only ``description`` is required.  Pass other kwargs to update those
    fields as well.  Returns the updated entity dict, or None if not found.
    """
    now = datetime.now().isoformat()
    fields = ["description = ?", "updated_at = ?"]
    params: list = [_sanitize_text(description.strip()), now]

    if subject is not None:
        fields.append("subject = ?")
        params.append(_sanitize_text(subject.strip()))
    if entity_type is not None:
        et = entity_type.lower().strip()
        if et in VALID_ENTITY_TYPES:
            fields.append("entity_type = ?")
            params.append(et)
    if aliases is not None:
        fields.append("aliases = ?")
        params.append(_sanitize_text(aliases.strip()))
    if tags is not None:
        fields.append("tags = ?")
        params.append(_sanitize_text(tags.strip()))
    if properties is not None:
        fields.append("properties = ?")
        params.append(json.dumps(properties))
    if source is not None:
        fields.append("source = ?")
        params.append(source.strip())

    params.append(entity_id)
    conn = _get_conn()
    cur = conn.execute(
        f"UPDATE entities SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        return None

    row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    conn.close()

    if row:
        entity = dict(row)
        # Update NetworkX node
        with _graph_lock:
            g = _ensure_graph()
            if entity_id in g:
                g.nodes[entity_id].update(entity)
            else:
                g.add_node(entity_id, **entity)
        if not _skip_reindex:
            _upsert_index(entity_id)
        # Wiki vault export (non-blocking)
        _wiki_export_entity(entity)
        return entity
    return None


def delete_entity(entity_id: str) -> bool:
    """Delete an entity and its relations.  Returns True if deleted."""
    # Capture entity data before deletion for wiki cleanup
    entity_data = get_entity(entity_id)

    conn = _get_conn()
    # FK CASCADE handles relations
    cur = conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
    conn.commit()
    conn.close()

    deleted = cur.rowcount > 0
    if deleted:
        with _graph_lock:
            g = _ensure_graph()
            if entity_id in g:
                g.remove_node(entity_id)  # also removes incident edges
        if not _skip_reindex:
            _remove_from_index(entity_id)
        # Wiki vault cleanup
        if entity_data:
            _wiki_delete_entity(entity_data)
    return deleted


def delete_entities_by_source(source: str) -> int:
    """Delete all entities (and their relations via FK CASCADE) matching *source*.

    Re-syncs the NetworkX graph and rebuilds the FAISS index once at the end.
    Returns the number of entities deleted.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, entity_type, subject, description FROM entities WHERE source = ?",
        (source,),
    ).fetchall()
    if not rows:
        conn.close()
        return 0

    ids = [r[0] for r in rows]
    conn.execute(
        f"DELETE FROM entities WHERE id IN ({','.join('?' * len(ids))})", ids,
    )
    # Also delete relations that reference this source (orphaned by other docs)
    conn.execute("DELETE FROM relations WHERE source = ?", (source,))
    conn.commit()
    conn.close()

    with _graph_lock:
        g = _ensure_graph()
        for eid in ids:
            if eid in g:
                g.remove_node(eid)

    if not _skip_reindex:
        rebuild_index()

    # Wiki vault cleanup
    for r in rows:
        _wiki_delete_entity(dict(zip(("id", "entity_type", "subject", "description"), r)))

    return len(ids)


def delete_entities_by_source_prefix(prefix: str) -> int:
    """Delete all entities whose source starts with *prefix* (e.g. ``'document:'``).

    Returns total entities deleted.
    """
    conn = _get_conn()
    sources = conn.execute(
        "SELECT DISTINCT source FROM entities WHERE source LIKE ?",
        (prefix + "%",),
    ).fetchall()
    conn.close()

    total = 0
    for (src,) in sources:
        total += delete_entities_by_source(src)
    return total


def list_entities(
    entity_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List entities, optionally filtered by type."""
    conn = _get_conn()
    if entity_type:
        entity_type = entity_type.lower().strip()
        rows = conn.execute(
            "SELECT * FROM entities WHERE entity_type = ? ORDER BY updated_at DESC LIMIT ?",
            (entity_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entities ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_entities() -> int:
    """Return total number of stored entities."""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    conn.close()
    return count


def search_entities(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Keyword search across subject, description, aliases, and tags."""
    conn = _get_conn()
    sql = (
        "SELECT * FROM entities WHERE "
        "(subject LIKE ? OR description LIKE ? OR aliases LIKE ? OR tags LIKE ?)"
    )
    params: list = [f"%{query}%"] * 4

    if entity_type:
        entity_type = entity_type.lower().strip()
        sql += " AND entity_type = ?"
        params.append(entity_type)

    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_by_subject(
    entity_type: str | None,
    subject: str,
) -> dict | None:
    """Find an entity by normalised subject (and optionally type).

    Deterministic SQL lookup — no embedding similarity.  Also checks
    the ``aliases`` field for alternative name matches.

    Returns the most recently updated match, or None.
    """
    conn = _get_conn()
    if entity_type is not None:
        et = entity_type.lower().strip()
        rows = conn.execute(
            "SELECT * FROM entities WHERE entity_type = ? ORDER BY updated_at DESC",
            (et,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entities ORDER BY updated_at DESC",
        ).fetchall()
    conn.close()

    norm = _normalize_subject(subject)

    matches = []
    for row in rows:
        row = dict(row)
        # Match on subject
        if _normalize_subject(row["subject"]) == norm:
            matches.append(row)
            continue
        # Match on aliases
        aliases = row.get("aliases", "")
        if aliases:
            for alias in aliases.split(","):
                if _normalize_subject(alias.strip()) == norm:
                    matches.append(row)
                    break

    if not matches:
        return None
    # For the canonical "User" entity, prefer person type
    if norm == "user":
        for m in matches:
            if m.get("entity_type") == "person":
                return m
    return matches[0]


# ── Auto-link helpers ────────────────────────────────────────────────────────

def _ensure_user_entity() -> str:
    """Return the ID of the canonical 'User' entity, creating it if needed."""
    existing = find_by_subject(None, "User")
    if existing:
        return existing["id"]
    entity = save_entity("person", "User", "The user of this system")
    return entity["id"]




def semantic_search(
    query: str,
    top_k: int = 5,
    threshold: float = 0.5,
) -> list[dict]:
    """Return the top-k entities most semantically similar to *query*.

    Each result dict has an extra ``score`` key (cosine similarity, 0–1).
    Only results with score >= *threshold* are returned.
    """
    import faiss as _faiss

    index_path = _VECTOR_DIR / "index.faiss"
    map_path = _VECTOR_DIR / "id_map.json"
    from embedding_config import index_metadata_matches

    if not index_path.exists() or not map_path.exists() or not index_metadata_matches(_VECTOR_DIR):
        rebuild_index()
    if not index_path.exists():
        return []

    with _faiss_lock:
        index = _faiss.read_index(str(index_path))
        if index.ntotal == 0:
            return []
        id_map: list[str] = json.loads(map_path.read_text())

    emb = _get_embedding_model()
    qvec = np.array(emb.embed_query(query), dtype=np.float32).reshape(1, -1)
    qvec = qvec / (np.linalg.norm(qvec) or 1)

    k = min(top_k, index.ntotal)
    scores, indices = index.search(qvec, k)

    results = []
    _seen_ids: set[str] = set()
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(id_map):
            continue
        if float(score) < threshold:
            continue
        eid = id_map[idx]
        if eid in _seen_ids:
            continue  # dedup stale vectors from incremental updates
        _seen_ids.add(eid)
        entity = get_entity(eid)
        if entity:
            entity["score"] = round(float(score), 4)
            results.append(entity)

    return results


def find_duplicate(
    entity_type: str,
    subject: str,
    description: str,
    threshold: float = 0.92,
) -> dict | None:
    """Find a near-duplicate entity by semantic similarity + subject match."""
    search_text = f"{entity_type} {subject} {description}"
    try:
        results = semantic_search(search_text, top_k=5, threshold=threshold)
    except Exception:
        return None
    norm_subj = _normalize_subject(subject)
    for e in results:
        if _normalize_subject(e.get("subject", "")) == norm_subj:
            return e
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Relation CRUD
# ═════════════════════════════════════════════════════════════════════════════

def add_relation(
    source_id: str,
    target_id: str,
    relation_type: str,
    *,
    confidence: float = 1.0,
    properties: dict | None = None,
    source: str = "live",
) -> dict | None:
    """Create a directed relation (edge) between two entities.

    Parameters
    ----------
    source_id, target_id : str
        Entity IDs.  Both must exist.
    relation_type : str
        Open label — e.g. ``'father_of'``, ``'lives_in'``, ``'works_on'``.
    confidence : float
        0.0–1.0 confidence score (1.0 = certain).
    properties : dict, optional
        Extra structured metadata on the relation.
    source : str
        ``'live'`` or ``'extraction'``.

    Returns
    -------
    dict  with all relation columns, or ``None`` if either entity is missing.
    """
    # Block self-loops (entity pointing to itself)
    if source_id == target_id:
        logger.debug("Rejected self-loop relation: %s --[%s]--> %s", source_id, relation_type, target_id)
        return None

    # Block vague/meaningless relation types
    _BANNED_RELATION_TYPES = {
        "related_to", "associated_with", "connected_to", "linked_to",
        "has_relation", "involves", "correlates_with",
    }
    _norm_check = normalize_relation_type(relation_type)
    if _norm_check in _BANNED_RELATION_TYPES:
        logger.debug(
            "Rejected vague relation type '%s': %s → %s",
            relation_type, source_id, target_id,
        )
        return None

    # Validate both endpoints exist
    conn = _get_conn()
    src = conn.execute("SELECT id FROM entities WHERE id = ?", (source_id,)).fetchone()
    tgt = conn.execute("SELECT id FROM entities WHERE id = ?", (target_id,)).fetchone()
    if not src or not tgt:
        conn.close()
        return None

    rel_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    props_json = json.dumps(properties or {})
    relation_type = normalize_relation_type(relation_type)
    confidence = max(0.0, min(1.0, confidence))

    # Warn on unknown relation types (but still accept to avoid breakage)
    if relation_type not in VALID_RELATION_TYPES:
        logger.warning(
            "Unknown relation type '%s' (%s → %s) — not in VALID_RELATION_TYPES",
            relation_type, source_id, target_id,
        )

    try:
        conn.execute(
            "INSERT INTO relations "
            "(id, source_id, target_id, relation_type, confidence, properties, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rel_id, source_id, target_id, relation_type, confidence,
             props_json, source, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Duplicate edge — already exists, nothing to do
        conn.close()
        return None

    conn.close()

    rel = {
        "id": rel_id,
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "confidence": confidence,
        "properties": props_json,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }

    # Update NetworkX (use relation ID as edge key for deterministic removal)
    with _graph_lock:
        g = _ensure_graph()
        g.add_edge(source_id, target_id, key=rel_id, **rel)

    # Re-export both endpoints so .md Connections sections stay current
    for eid in (source_id, target_id):
        ent = get_entity(eid)
        if ent:
            _wiki_export_entity(ent)

    return rel


def get_relations(
    entity_id: str,
    direction: str = "both",
) -> list[dict]:
    """Get all relations involving an entity.

    Parameters
    ----------
    entity_id : str
        The entity to query.
    direction : str
        ``'outgoing'`` (entity is source), ``'incoming'`` (entity is target),
        or ``'both'`` (default).

    Returns
    -------
    list[dict]  — each dict has all relation columns plus ``peer_id`` and
    ``peer_subject`` for convenience.
    """
    conn = _get_conn()
    results = []

    if direction in ("outgoing", "both"):
        rows = conn.execute(
            "SELECT r.*, e.subject AS peer_subject FROM relations r "
            "JOIN entities e ON e.id = r.target_id "
            "WHERE r.source_id = ? ORDER BY r.updated_at DESC",
            (entity_id,),
        ).fetchall()
        for row in rows:
            d = dict(row)
            d["peer_id"] = d["target_id"]
            d["direction"] = "outgoing"
            results.append(d)

    if direction in ("incoming", "both"):
        rows = conn.execute(
            "SELECT r.*, e.subject AS peer_subject FROM relations r "
            "JOIN entities e ON e.id = r.source_id "
            "WHERE r.target_id = ? ORDER BY r.updated_at DESC",
            (entity_id,),
        ).fetchall()
        for row in rows:
            d = dict(row)
            d["peer_id"] = d["source_id"]
            d["direction"] = "incoming"
            results.append(d)

    conn.close()
    return results


def delete_relation(relation_id: str) -> bool:
    """Delete a relation by ID.  Returns True if deleted."""
    conn = _get_conn()
    # Read before delete so we can update NetworkX
    row = conn.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
    if not row:
        conn.close()
        return False
    row = dict(row)
    conn.execute("DELETE FROM relations WHERE id = ?", (relation_id,))
    conn.commit()
    conn.close()

    with _graph_lock:
        g = _ensure_graph()
        src, tgt = row["source_id"], row["target_id"]
        # MultiDiGraph: remove by key (relation ID) to preserve parallel edges
        if g.has_edge(src, tgt, key=relation_id):
            g.remove_edge(src, tgt, key=relation_id)
        elif g.has_edge(src, tgt):
            # Fallback: edge exists but key doesn't match (legacy data)
            # Find the edge with matching relation id
            edge_keys = [k for k, d in g[src][tgt].items() if d.get("id") == relation_id]
            for k in edge_keys:
                g.remove_edge(src, tgt, key=k)

    # Re-export both endpoints so .md Connections sections stay current
    for eid in (row["source_id"], row["target_id"]):
        ent = get_entity(eid)
        if ent:
            _wiki_export_entity(ent)

    return True


def count_relations() -> int:
    """Return total number of stored relations."""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    conn.close()
    return count


def list_relations(limit: int = 100) -> list[dict]:
    """List all relations with entity subjects for readability."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT r.*, "
        "  s.subject AS source_subject, "
        "  t.subject AS target_subject "
        "FROM relations r "
        "JOIN entities s ON s.id = r.source_id "
        "JOIN entities t ON t.id = r.target_id "
        "ORDER BY r.updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
# Graph query helpers
# ═════════════════════════════════════════════════════════════════════════════

def get_neighbors(
    entity_id: str,
    hops: int = 1,
    direction: str = "both",
) -> list[dict]:
    """Return entities within *hops* of *entity_id* in the graph.

    Parameters
    ----------
    entity_id : str
    hops : int
        Number of edges to traverse (1 = immediate neighbors).
    direction : str
        ``'outgoing'``, ``'incoming'``, or ``'both'``.

    Returns list of entity dicts with an extra ``hop`` key.
    """
    g = _ensure_graph()
    if entity_id not in g:
        return []

    visited: dict[str, int] = {entity_id: 0}
    frontier = [entity_id]

    for depth in range(1, hops + 1):
        next_frontier = []
        for nid in frontier:
            neighbors = set()
            if direction in ("outgoing", "both"):
                neighbors.update(g.successors(nid))
            if direction in ("incoming", "both"):
                neighbors.update(g.predecessors(nid))
            for nbr in neighbors:
                if nbr not in visited:
                    visited[nbr] = depth
                    next_frontier.append(nbr)
        frontier = next_frontier

    results = []
    for nid, hop in visited.items():
        if nid == entity_id:
            continue
        node_data = g.nodes.get(nid, {})
        if node_data:
            entity = dict(node_data)
            entity["hop"] = hop
            results.append(entity)

    # Sort by hop distance, then by update time
    results.sort(key=lambda e: (e["hop"], e.get("updated_at", "")))
    return results


def get_shortest_path(
    source_id: str,
    target_id: str,
) -> list[dict] | None:
    """Return the shortest path between two entities as a list of entity dicts.

    Returns None if no path exists.  Uses the undirected view of the graph.
    """
    g = _ensure_graph()
    if source_id not in g or target_id not in g:
        return None

    try:
        path = nx.shortest_path(g.to_undirected(), source_id, target_id)
    except nx.NetworkXNoPath:
        return None

    return [dict(g.nodes[nid]) for nid in path if g.nodes.get(nid)]


def get_subgraph(entity_id: str, hops: int = 2) -> dict:
    """Extract a subgraph around an entity for visualisation.

    Returns
    -------
    dict with keys:
        ``nodes`` — list of entity dicts
        ``edges`` — list of relation dicts with source_subject/target_subject
    """
    g = _ensure_graph()
    if entity_id not in g:
        return {"nodes": [], "edges": []}

    neighbors = get_neighbors(entity_id, hops=hops)
    node_ids = {entity_id} | {n["id"] for n in neighbors}

    nodes = []
    center = g.nodes.get(entity_id)
    if center:
        nodes.append(dict(center))
    nodes.extend(neighbors)

    edges = []
    for u, v, data in g.edges(data=True):
        if u in node_ids and v in node_ids:
            edge = dict(data)
            edge["source_id"] = u
            edge["target_id"] = v
            edge["source_subject"] = g.nodes[u].get("subject", u)
            edge["target_subject"] = g.nodes[v].get("subject", v)
            edges.append(edge)

    return {"nodes": nodes, "edges": edges}


def get_connected_components() -> list[list[str]]:
    """Return connected components as lists of entity IDs (largest first)."""
    g = _ensure_graph()
    undirected = g.to_undirected()
    components = sorted(nx.connected_components(undirected), key=len, reverse=True)
    return [list(c) for c in components]


def get_graph_stats() -> dict:
    """Return summary statistics about the knowledge graph."""
    g = _ensure_graph()
    conn = _get_conn()

    # Entity type breakdown
    type_counts = {}
    for row in conn.execute(
        "SELECT entity_type, COUNT(*) as cnt FROM entities GROUP BY entity_type"
    ).fetchall():
        type_counts[row[0]] = row[1]

    # Relation type breakdown
    rel_counts = {}
    for row in conn.execute(
        "SELECT relation_type, COUNT(*) as cnt FROM relations GROUP BY relation_type"
    ).fetchall():
        rel_counts[row[0]] = row[1]

    conn.close()

    components = get_connected_components()

    return {
        "total_entities": g.number_of_nodes(),
        "total_relations": g.number_of_edges(),
        "entity_types": type_counts,
        "relation_types": rel_counts,
        "connected_components": len(components),
        "largest_component": len(components[0]) if components else 0,
        "isolated_entities": sum(1 for c in components if len(c) == 1),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Mermaid export
# ═════════════════════════════════════════════════════════════════════════════

def _mermaid_safe(text: str) -> str:
    """Escape text for Mermaid labels."""
    return text.replace('"', "'").replace("\n", " ")[:50]


def to_mermaid(
    entity_id: str | None = None,
    hops: int = 2,
    max_nodes: int = 30,
) -> str:
    """Generate a Mermaid graph diagram.

    If *entity_id* is given, shows the local subgraph.  Otherwise shows
    the full graph (capped at *max_nodes* most-connected entities).

    Returns a Mermaid string like::

        graph LR
            a123["Mom (person)"] -->|mother_of| b456["User (person)"]
    """
    g = _ensure_graph()
    lines = ["graph LR"]

    if entity_id and entity_id in g:
        sub = get_subgraph(entity_id, hops=hops)
        nodes = sub["nodes"][:max_nodes]
        node_ids = {n["id"] for n in nodes}
        for n in nodes:
            label = _mermaid_safe(f"{n.get('subject', '?')} ({n.get('entity_type', '?')})")
            lines.append(f'    {n["id"]}["{label}"]')
        for e in sub["edges"]:
            if e.get("source_id") in node_ids and e.get("target_id") in node_ids:
                rel = _mermaid_safe(e.get("relation_type", "related"))
                lines.append(f'    {e["source_id"]} -->|{rel}| {e["target_id"]}')
    else:
        # Full graph, pick top N by degree
        degree_sorted = sorted(g.nodes, key=lambda n: g.degree(n), reverse=True)[:max_nodes]
        node_ids = set(degree_sorted)
        for nid in degree_sorted:
            data = g.nodes.get(nid, {})
            label = _mermaid_safe(f"{data.get('subject', '?')} ({data.get('entity_type', '?')})")
            lines.append(f'    {nid}["{label}"]')
        for u, v, data in g.edges(data=True):
            if u in node_ids and v in node_ids:
                rel = _mermaid_safe(data.get("relation_type", "related"))
                lines.append(f"    {u} -->|{rel}| {v}")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# vis-network JSON serialization (used by the UI graph tab)
# ═════════════════════════════════════════════════════════════════════════════

# Color palette for entity types — muted, readable on dark backgrounds.
_VIS_TYPE_COLORS: dict[str, str] = {
    "person":       "#4FC3F7",   # light blue
    "preference":   "#FFD54F",   # amber
    "fact":         "#81C784",   # green
    "event":        "#FF8A65",   # deep orange
    "place":        "#BA68C8",   # purple
    "project":      "#4DB6AC",   # teal
    "organisation": "#A1887F",   # brown
    "concept":      "#90A4AE",   # blue-grey
    "skill":        "#F06292",   # pink
    "media":        "#AED581",   # light green
    "self_knowledge": "#7E57C2", # deep purple
}
_VIS_DEFAULT_COLOR = "#B0BEC5"  # grey fallback


def graph_to_vis_json(
    entity_id: str | None = None,
    hops: int = 2,
    max_nodes: int = 500,
) -> dict:
    """Serialize the graph (or a subgraph) into vis-network JSON format.

    Parameters
    ----------
    entity_id
        If given, returns the *hops*-hop neighborhood around this entity.
        If ``None``, returns the full graph (degree-sorted, capped at
        *max_nodes*).
    hops
        Neighborhood radius when *entity_id* is provided.
    max_nodes
        Hard cap on node count for the full-graph mode.

    Returns
    -------
    dict with keys:
        ``nodes`` — list of vis-network node objects
        ``edges`` — list of vis-network edge objects
        ``center`` — entity_id of the center node (or highest-degree node)
        ``stats`` — ``{total_entities, total_relations, shown_nodes, shown_edges}``
    """
    g = _ensure_graph()

    if entity_id and entity_id in g:
        # ── Local subgraph mode ──────────────────────────────────────────
        sub = get_subgraph(entity_id, hops=hops)
        raw_nodes = sub["nodes"]
        raw_edges = sub["edges"]
        center_id = entity_id
    else:
        # ── Full graph mode (degree-sorted, capped) ─────────────────────
        if g.number_of_nodes() == 0:
            return {
                "nodes": [], "edges": [], "center": None,
                "stats": {"total_entities": 0, "total_relations": 0,
                          "shown_nodes": 0, "shown_edges": 0},
            }
        degree_sorted = sorted(
            g.nodes, key=lambda n: g.degree(n), reverse=True,
        )[:max_nodes]
        node_ids = set(degree_sorted)

        raw_nodes = [dict(g.nodes[nid]) for nid in degree_sorted if g.nodes.get(nid)]
        raw_edges = []
        for u, v, data in g.edges(data=True):
            if u in node_ids and v in node_ids:
                edge = dict(data)
                edge["source_id"] = u
                edge["target_id"] = v
                edge["source_subject"] = g.nodes[u].get("subject", u)
                edge["target_subject"] = g.nodes[v].get("subject", v)
                raw_edges.append(edge)

        # Center = "User" entity if present, else highest-degree node
        center_id = degree_sorted[0]
        for nid in degree_sorted:
            subj = g.nodes[nid].get("subject", "")
            if subj.lower() == "user":
                center_id = nid
                break

    # ── Build vis-network nodes ──────────────────────────────────────────
    # Compute degree range for sizing
    node_ids_set = {n["id"] for n in raw_nodes}
    degrees = {n["id"]: g.degree(n["id"]) for n in raw_nodes if n["id"] in g}
    min_deg = min(degrees.values()) if degrees else 0
    max_deg = max(degrees.values()) if degrees else 0
    deg_range = max_deg - min_deg if max_deg > min_deg else 1

    vis_nodes = []
    for n in raw_nodes:
        nid = n["id"]
        etype = n.get("entity_type", "")
        subject = n.get("subject", "?")
        color = _VIS_TYPE_COLORS.get(etype, _VIS_DEFAULT_COLOR)

        # Size: 15–40 based on degree
        deg = degrees.get(nid, 0)
        size = 15 + int(25 * (deg - min_deg) / deg_range)

        desc = n.get("description", "") or ""
        aliases = n.get("aliases", "") or ""
        tags = n.get("tags", "") or ""
        source = n.get("source", "live") or "live"
        updated_at = n.get("updated_at", "") or ""

        vis_nodes.append({
            "id": nid,
            "label": subject,
            "color": color,
            "size": size,
            "font": {"color": "#ECEFF1"},
            "title": (
                f"{subject}\n"
                f"Type: {etype}\n"
                f"Connections: {deg}"
                + (f"\n{desc[:120]}" if desc else "")
            ),
            # Extra data for the detail card and filtering
            "_type": etype,
            "_description": desc,
            "_aliases": aliases,
            "_tags": tags,
            "_degree": deg,
            "_source": source,
            "_updated_at": updated_at,
        })

    # ── Build vis-network edges ──────────────────────────────────────────
    vis_edges = []
    for e in raw_edges:
        src = e.get("source_id", "")
        tgt = e.get("target_id", "")
        if src not in node_ids_set or tgt not in node_ids_set:
            continue
        rel = e.get("relation_type", "")
        vis_edges.append({
            "id": f"{src}__{tgt}__{rel}",
            "from": src,
            "to": tgt,
            "label": rel,
            "arrows": "to",
            "color": {"color": "#616161", "highlight": "#FFD54F"},
        })

    return {
        "nodes": vis_nodes,
        "edges": vis_edges,
        "center": center_id,
        "stats": {
            "total_entities": g.number_of_nodes(),
            "total_relations": g.number_of_edges(),
            "shown_nodes": len(vis_nodes),
            "shown_edges": len(vis_edges),
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
# Memory decay & recall reinforcement
# ═════════════════════════════════════════════════════════════════════════════

def _decay_multiplier(entity: dict) -> float:
    """Return a decay factor (0.7–1.0) based on recency of access/update.

    Mimics human memory: recently accessed or updated memories stay vivid,
    while unused ones gradually fade.  Recalling a memory refreshes it
    (the *testing effect*).

    * Within 7 days  → 1.0 (no decay)
    * 7–90 days      → linear from 1.0 → 0.7
    * 90+ days       → 0.7 (floor — never fully forgotten)
    """
    props = entity.get("properties", "{}")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (json.JSONDecodeError, TypeError):
            props = {}

    recalled_at = props.get("recalled_at", "") if isinstance(props, dict) else ""
    updated_at = entity.get("updated_at", "")

    # Use the most recent of recalled_at and updated_at
    fresh_ts = max(recalled_at, updated_at) if recalled_at else updated_at
    if not fresh_ts:
        return 0.7

    try:
        fresh_dt = datetime.fromisoformat(fresh_ts)
        days_old = (datetime.now() - fresh_dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.85

    if days_old <= 7:
        return 1.0
    if days_old >= 90:
        return 0.7
    # Linear decay: 1.0 at day 7 → 0.7 at day 90
    return 1.0 - 0.3 * (days_old - 7) / 83


def _touch_recalled(entity_ids: list[str]) -> None:
    """Update ``recalled_at`` in properties for entities just recalled.

    This 'refreshes' memories in the decay system — mimicking how human
    memory strengthens through recall (the *testing effect*).
    """
    if not entity_ids:
        return
    now = datetime.now().isoformat()
    conn = _get_conn()
    for eid in entity_ids:
        row = conn.execute(
            "SELECT properties FROM entities WHERE id = ?", (eid,)
        ).fetchone()
        if row:
            try:
                props = json.loads(row[0] or "{}")
            except (json.JSONDecodeError, TypeError):
                props = {}
            props["recalled_at"] = now
            conn.execute(
                "UPDATE entities SET properties = ? WHERE id = ?",
                (json.dumps(props), eid),
            )
    conn.commit()
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Graph-enhanced recall (used by agent.py auto-recall)
# ═════════════════════════════════════════════════════════════════════════════

def graph_enhanced_recall(
    query: str,
    top_k: int = 5,
    threshold: float = 0.35,
    hops: int = 1,
    max_results: int = 20,
) -> list[dict]:
    """Semantic search + memory decay + graph expansion + wiki fallback.

    1. FAISS semantic search for top-k seed entities.
    2. Apply memory decay multiplier (recent/recalled → higher score).
    3. Expand 1-hop neighbors from the graph (scored relative to seed).
    4. Wiki vault full-text fallback when FAISS returns few results.
    5. Cap total results to *max_results* to control token usage.
    6. Reinforce recalled memories (touch ``recalled_at``).

    Each returned entity has extra keys:
        ``score`` — semantic similarity × decay (seeds), or derived (graph/wiki)
        ``via`` — ``'semantic'``, ``'graph'``, or ``'wiki'``
        ``relations`` — list of relations connecting this entity to its seed
    """
    seeds = semantic_search(query, top_k=top_k, threshold=threshold)
    if not seeds:
        return []

    # Apply memory decay — recently accessed memories score higher
    for seed in seeds:
        seed["score"] = round(seed.get("score", 0) * _decay_multiplier(seed), 4)

    # Re-filter after decay — use a softer floor so old-but-relevant
    # memories fade in ranking but don't vanish entirely.
    decay_floor = threshold * 0.7
    seeds = [s for s in seeds if s["score"] >= decay_floor]
    if not seeds:
        return []

    seeds.sort(key=lambda s: s["score"], reverse=True)

    g = _ensure_graph()
    seen_ids = {s["id"] for s in seeds}
    result = []

    for seed in seeds:
        seed["via"] = "semantic"
        seed["relations"] = []
        result.append(seed)

        # 1-hop expansion
        neighbors = get_neighbors(seed["id"], hops=hops)
        for nbr in neighbors:
            if nbr["id"] in seen_ids:
                continue
            seen_ids.add(nbr["id"])

            # Collect the relations that connect this neighbor to the seed
            connecting_rels = []
            if g.has_edge(seed["id"], nbr["id"]):
                # MultiDiGraph: g[u][v] is {key: data_dict, ...}
                for _ekey, edata in g[seed["id"]][nbr["id"]].items():
                    connecting_rels.append({
                        "from": seed.get("subject", ""),
                        "to": nbr.get("subject", ""),
                        "type": edata.get("relation_type", "related"),
                    })
            if g.has_edge(nbr["id"], seed["id"]):
                for _ekey, edata in g[nbr["id"]][seed["id"]].items():
                    connecting_rels.append({
                        "from": nbr.get("subject", ""),
                        "to": seed.get("subject", ""),
                        "type": edata.get("relation_type", "related"),
                    })

            # Derive neighbor score from its seed (halved) so neighbors
            # sort meaningfully instead of all being 0.
            nbr["score"] = round(seed["score"] * 0.5, 4)
            nbr["via"] = "graph"
            nbr["relations"] = connecting_rels
            result.append(nbr)

    # ── SQL LIKE fallback ──────────────────────────────────────────
    # Always run a keyword search alongside FAISS to catch exact names,
    # aliases, tags, and coded terms that embedding models miss.
    # Negligible cost (~<1 ms on ~200 rows).
    try:
        sql_hits = search_entities(query, limit=10)
        for entity in sql_hits:
            if entity["id"] in seen_ids:
                continue
            seen_ids.add(entity["id"])
            entity["score"] = 0.10  # low but present
            entity["via"] = "keyword"
            entity["relations"] = []
            result.append(entity)
    except Exception:
        pass  # keyword fallback is non-critical

    # ── Cap total results to control token usage ────────────────────
    result.sort(key=lambda m: m["score"], reverse=True)
    result = result[:max_results]

    # Reinforce recalled memories (touch recalled_at timestamp)
    try:
        _touch_recalled([m["id"] for m in result])
    except Exception:
        pass  # non-critical — don't break recall if touch fails

    return result


# ═════════════════════════════════════════════════════════════════════════════
# Bulk operations
# ═════════════════════════════════════════════════════════════════════════════

def delete_all_entities() -> int:
    """Delete every entity and relation.  Returns entity count deleted."""
    conn = _get_conn()
    conn.execute("DELETE FROM relations")
    cur = conn.execute("DELETE FROM entities")
    conn.commit()
    conn.close()
    count = cur.rowcount

    global _graph, _graph_ready
    with _graph_lock:
        _graph = nx.MultiDiGraph()
        _graph_ready = True

    if count:
        rebuild_index()

    # Clean wiki vault files
    try:
        import wiki_vault
        wiki_vault.clear_wiki_folder()
    except Exception as exc:
        logger.debug("Wiki cleanup skipped: %s", exc)

    return count


def consolidate_duplicates(threshold: float = 0.90) -> int:
    """Scan all entities and merge near-duplicates by subject.

    For each pair sharing the same normalised subject and a semantic
    similarity score >= *threshold*, the shorter/older entry is merged
    into the longer/newer one and then deleted.

    Returns the number of entities removed.
    """
    all_entities = list_entities(limit=100_000)
    if len(all_entities) < 2:
        return 0

    # Group by normalised subject
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in all_entities:
        key = _normalize_subject(e["subject"])
        groups[key].append(e)

    removed = 0
    for _subj, entities in groups.items():
        if len(entities) < 2:
            continue

        deleted_ids: set[str] = set()
        for i, e1 in enumerate(entities):
            if e1["id"] in deleted_ids:
                continue
            for e2 in entities[i + 1:]:
                if e2["id"] in deleted_ids:
                    continue

                text1 = f"{e1['entity_type']} {e1['subject']} {e1['description']}"
                try:
                    hits = semantic_search(text1, top_k=5, threshold=threshold)
                except Exception:
                    continue

                hit_ids = {h["id"] for h in hits}
                if e2["id"] not in hit_ids:
                    continue

                # Near-duplicates — keep the richer one
                keep, drop = (
                    (e1, e2)
                    if len(e1.get("description", "")) >= len(e2.get("description", ""))
                    else (e2, e1)
                )

                # Merge tags
                merged_tags = ", ".join(
                    dict.fromkeys(
                        t.strip()
                        for t in (keep.get("tags", "") + "," + drop.get("tags", "")).split(",")
                        if t.strip()
                    )
                )

                # Merge aliases
                merged_aliases = ", ".join(
                    dict.fromkeys(
                        a.strip()
                        for a in (keep.get("aliases", "") + "," + drop.get("aliases", "")).split(",")
                        if a.strip()
                    )
                )

                # Merge properties
                keep_props = json.loads(keep.get("properties", "{}")) if isinstance(keep.get("properties"), str) else keep.get("properties", {})
                drop_props = json.loads(drop.get("properties", "{}")) if isinstance(drop.get("properties"), str) else drop.get("properties", {})
                merged_props = {**drop_props, **keep_props}  # keep's values win

                update_entity(
                    keep["id"],
                    keep["description"],
                    tags=merged_tags,
                    aliases=merged_aliases,
                    properties=merged_props,
                )

                # Re-point drop's relations to keep
                conn = _get_conn()
                for rel in conn.execute(
                    "SELECT * FROM relations WHERE source_id = ?", (drop["id"],)
                ).fetchall():
                    rel = dict(rel)
                    try:
                        conn.execute(
                            "UPDATE relations SET source_id = ?, updated_at = ? WHERE id = ?",
                            (keep["id"], datetime.now().isoformat(), rel["id"]),
                        )
                    except sqlite3.IntegrityError:
                        conn.execute("DELETE FROM relations WHERE id = ?", (rel["id"],))
                for rel in conn.execute(
                    "SELECT * FROM relations WHERE target_id = ?", (drop["id"],)
                ).fetchall():
                    rel = dict(rel)
                    try:
                        conn.execute(
                            "UPDATE relations SET target_id = ?, updated_at = ? WHERE id = ?",
                            (keep["id"], datetime.now().isoformat(), rel["id"]),
                        )
                    except sqlite3.IntegrityError:
                        conn.execute("DELETE FROM relations WHERE id = ?", (rel["id"],))
                conn.commit()
                conn.close()

                delete_entity(drop["id"])
                deleted_ids.add(drop["id"])
                removed += 1
                logger.info(
                    "Consolidated duplicate: kept %s (%s), removed %s",
                    keep["id"], keep["subject"], drop["id"],
                )

    # Reload graph after bulk consolidation
    if removed:
        _load_graph()

    return removed





# ═════════════════════════════════════════════════════════════════════════════
# Load graph on import (but lazily — only when first accessed)
# ═════════════════════════════════════════════════════════════════════════════

# We defer _load_graph() to first access via _ensure_graph() — this avoids
# blocking import time when the embedding model or FAISS aren't needed yet.
