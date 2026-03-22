"""Personal Knowledge Graph — entity-relation graph with SQLite + NetworkX.

Replaces the flat ``memories`` table with a connected graph of **entities**
(people, places, facts, preferences, events, projects, …) and **relations**
(edges like ``father_of``, ``lives_in``, ``works_on``).

Architecture
~~~~~~~~~~~~
* **SQLite** is the durable store (WAL mode, same ``~/.thoth/memory.db``).
* **NetworkX** ``DiGraph`` is an in-memory mirror rebuilt on startup from
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
import sqlite3
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
    "organisation", "concept", "skill", "media",
}

# Keep backward compat alias
VALID_CATEGORIES = VALID_ENTITY_TYPES


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


# ═════════════════════════════════════════════════════════════════════════════
# NetworkX in-memory graph
# ═════════════════════════════════════════════════════════════════════════════

_graph: nx.DiGraph = nx.DiGraph()
_graph_ready = False


def _load_graph() -> None:
    """Populate the NetworkX graph from SQLite.  Called once at startup."""
    global _graph, _graph_ready
    _graph = nx.DiGraph()
    conn = _get_conn()

    # Load entities as nodes
    for row in conn.execute("SELECT * FROM entities").fetchall():
        row = dict(row)
        _graph.add_node(row["id"], **row)

    # Load relations as edges
    for row in conn.execute("SELECT * FROM relations").fetchall():
        row = dict(row)
        if row["source_id"] in _graph and row["target_id"] in _graph:
            _graph.add_edge(
                row["source_id"],
                row["target_id"],
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


def _ensure_graph() -> nx.DiGraph:
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

    entities = list_entities(limit=100_000)
    _VECTOR_DIR.mkdir(parents=True, exist_ok=True)

    if not entities:
        emb = _get_embedding_model()
        dim = len(emb.embed_query("test"))
        index = _faiss.IndexFlatIP(dim)
        with _faiss_lock:
            _faiss.write_index(index, str(_VECTOR_DIR / "index.faiss"))
            (_VECTOR_DIR / "id_map.json").write_text("[]")
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
    logger.info("Rebuilt FAISS index with %d entities", len(id_map))


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

    entity_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    props_json = json.dumps(properties or {})

    conn = _get_conn()
    conn.execute(
        "INSERT INTO entities "
        "(id, entity_type, subject, description, aliases, tags, properties, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, entity_type, subject.strip(), description.strip(),
         aliases.strip(), tags.strip(), props_json, source.strip(), now, now),
    )
    conn.commit()
    conn.close()

    entity = {
        "id": entity_id,
        "entity_type": entity_type,
        "subject": subject.strip(),
        "description": description.strip(),
        "aliases": aliases.strip(),
        "tags": tags.strip(),
        "properties": props_json,
        "source": source.strip(),
        "created_at": now,
        "updated_at": now,
    }

    # Update NetworkX
    g = _ensure_graph()
    g.add_node(entity_id, **entity)

    # Update FAISS
    rebuild_index()

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
    params: list = [description.strip(), now]

    if subject is not None:
        fields.append("subject = ?")
        params.append(subject.strip())
    if entity_type is not None:
        et = entity_type.lower().strip()
        if et in VALID_ENTITY_TYPES:
            fields.append("entity_type = ?")
            params.append(et)
    if aliases is not None:
        fields.append("aliases = ?")
        params.append(aliases.strip())
    if tags is not None:
        fields.append("tags = ?")
        params.append(tags.strip())
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
        g = _ensure_graph()
        if entity_id in g:
            g.nodes[entity_id].update(entity)
        else:
            g.add_node(entity_id, **entity)
        rebuild_index()
        return entity
    return None


def delete_entity(entity_id: str) -> bool:
    """Delete an entity and its relations.  Returns True if deleted."""
    conn = _get_conn()
    # FK CASCADE handles relations
    cur = conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
    conn.commit()
    conn.close()

    deleted = cur.rowcount > 0
    if deleted:
        g = _ensure_graph()
        if entity_id in g:
            g.remove_node(entity_id)  # also removes incident edges
        rebuild_index()
    return deleted


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

    for row in rows:
        row = dict(row)
        # Match on subject
        if _normalize_subject(row["subject"]) == norm:
            return row
        # Match on aliases
        aliases = row.get("aliases", "")
        if aliases:
            for alias in aliases.split(","):
                if _normalize_subject(alias.strip()) == norm:
                    return row
    return None


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

    if not index_path.exists() or not map_path.exists():
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
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(id_map):
            continue
        if float(score) < threshold:
            continue
        entity = get_entity(id_map[idx])
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
    relation_type = relation_type.lower().strip().replace(" ", "_")
    confidence = max(0.0, min(1.0, confidence))

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
        # Duplicate edge — update instead
        conn.execute(
            "UPDATE relations SET confidence = ?, properties = ?, source = ?, updated_at = ? "
            "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
            (confidence, props_json, source, now, source_id, target_id, relation_type),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM relations WHERE source_id = ? AND target_id = ? AND relation_type = ?",
            (source_id, target_id, relation_type),
        ).fetchone()
        conn.close()
        if row:
            rel = dict(row)
            # Update NetworkX edge
            g = _ensure_graph()
            if g.has_edge(source_id, target_id):
                g[source_id][target_id].update(
                    relation_type=relation_type,
                    confidence=confidence,
                    properties=props_json,
                    updated_at=now,
                )
            return rel
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

    # Update NetworkX
    g = _ensure_graph()
    g.add_edge(source_id, target_id, **rel)

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

    g = _ensure_graph()
    if g.has_edge(row["source_id"], row["target_id"]):
        g.remove_edge(row["source_id"], row["target_id"])
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
            # Extra data for the detail card
            "_type": etype,
            "_description": desc,
            "_aliases": aliases,
            "_tags": tags,
            "_degree": deg,
        })

    # ── Build vis-network edges ──────────────────────────────────────────
    vis_edges = []
    for e in raw_edges:
        src = e.get("source_id", "")
        tgt = e.get("target_id", "")
        if src not in node_ids_set or tgt not in node_ids_set:
            continue
        vis_edges.append({
            "from": src,
            "to": tgt,
            "label": e.get("relation_type", ""),
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
# Graph-enhanced recall (used by agent.py auto-recall)
# ═════════════════════════════════════════════════════════════════════════════

def graph_enhanced_recall(
    query: str,
    top_k: int = 5,
    threshold: float = 0.35,
    hops: int = 1,
) -> list[dict]:
    """Semantic search + 1-hop graph expansion for richer auto-recall.

    1. Run FAISS semantic search to get top-k seed entities.
    2. For each seed, collect 1-hop neighbors from the graph.
    3. Deduplicate and return seeds + neighbors with relation context.

    Each returned entity has extra keys:
        ``score`` — semantic similarity (seeds only, 0 for graph-expanded)
        ``via`` — ``'semantic'`` or ``'graph'``
        ``relations`` — list of relations connecting this entity to its seed
    """
    seeds = semantic_search(query, top_k=top_k, threshold=threshold)
    if not seeds:
        return []

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
                edata = g[seed["id"]][nbr["id"]]
                connecting_rels.append({
                    "from": seed.get("subject", ""),
                    "to": nbr.get("subject", ""),
                    "type": edata.get("relation_type", "related"),
                })
            if g.has_edge(nbr["id"], seed["id"]):
                edata = g[nbr["id"]][seed["id"]]
                connecting_rels.append({
                    "from": nbr.get("subject", ""),
                    "to": seed.get("subject", ""),
                    "type": edata.get("relation_type", "related"),
                })

            nbr["score"] = 0.0
            nbr["via"] = "graph"
            nbr["relations"] = connecting_rels
            result.append(nbr)

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
    _graph = nx.DiGraph()
    _graph_ready = True

    if count:
        rebuild_index()
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
