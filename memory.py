"""Long-term memory persistence layer — SQLite + FAISS vector search.

Stores facts, preferences, people, events, places, and projects that the
agent can recall across conversations.  Each memory has a category, subject,
free-text content, and optional comma-separated tags for flexible search.

Memories are *also* embedded in a FAISS index for semantic similarity search
and auto-recall.  The embedding model is shared with ``documents.py``.

Database lives at ``~/.thoth/memory.db`` (separate from threads).
FAISS index lives at ``~/.thoth/memory_vectors/``.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
import uuid
from datetime import datetime

import numpy as np

logger = logging.getLogger(__name__)

# ── Data directory ───────────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(_DATA_DIR / "memory.db")
_VECTOR_DIR = _DATA_DIR / "memory_vectors"

VALID_CATEGORIES = {"person", "preference", "fact", "event", "place", "project"}


# ── Schema bootstrap ────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """Return a connection with WAL mode and row-factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id         TEXT PRIMARY KEY,
            category   TEXT NOT NULL,
            subject    TEXT NOT NULL,
            content    TEXT NOT NULL,
            tags       TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT 'live',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject)"
    )
    # ── Migration: add source column to pre-existing databases ────────
    try:
        conn.execute("ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT 'live'")
        logger.info("Migrated memories table: added 'source' column")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


_init_db()


# ── Embedding & FAISS index ─────────────────────────────────────────────────

def _get_embedding_model():
    """Return the shared HuggingFaceEmbeddings instance from documents.py."""
    from documents import get_embedding_model
    return get_embedding_model()


def _memory_text(row: dict) -> str:
    """Build the string that gets embedded for a memory."""
    parts = [row["category"], row["subject"], row["content"]]
    if row.get("tags"):
        parts.append(row["tags"])
    return " | ".join(parts)


def _rebuild_memory_index() -> None:
    """(Re)build the FAISS index from all memories in SQLite."""
    import faiss as _faiss

    rows = list_memories(limit=100_000)
    if not rows:
        # Empty index — create a placeholder so load doesn't fail
        _VECTOR_DIR.mkdir(parents=True, exist_ok=True)
        emb = _get_embedding_model()
        dim = len(emb.embed_query("test"))
        index = _faiss.IndexFlatIP(dim)
        _faiss.write_index(index, str(_VECTOR_DIR / "index.faiss"))
        # Clear id map
        import json as _json
        (_VECTOR_DIR / "id_map.json").write_text("[]")
        return

    emb = _get_embedding_model()
    texts = [_memory_text(r) for r in rows]
    vectors = emb.embed_documents(texts)
    arr = np.array(vectors, dtype=np.float32)
    # Normalise for cosine similarity via inner product
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1
    arr = arr / norms

    dim = arr.shape[1]
    index = _faiss.IndexFlatIP(dim)
    index.add(arr)

    _VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    _faiss.write_index(index, str(_VECTOR_DIR / "index.faiss"))

    # Map FAISS row position → memory id
    import json as _json
    id_map = [r["id"] for r in rows]
    (_VECTOR_DIR / "id_map.json").write_text(_json.dumps(id_map))
    logger.info("Rebuilt memory FAISS index with %d entries", len(id_map))


def semantic_search(query: str, top_k: int = 5, threshold: float = 0.5) -> list[dict]:
    """Return the top-k memories most semantically similar to *query*.

    Each result dict has an extra ``score`` key (cosine similarity, 0-1).
    Only results with score >= *threshold* are returned.
    """
    import faiss as _faiss
    import json as _json

    index_path = _VECTOR_DIR / "index.faiss"
    map_path = _VECTOR_DIR / "id_map.json"

    if not index_path.exists() or not map_path.exists():
        _rebuild_memory_index()
    if not index_path.exists():
        return []

    index = _faiss.read_index(str(index_path))
    if index.ntotal == 0:
        return []

    id_map: list[str] = _json.loads(map_path.read_text())

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
        mem = get_memory(id_map[idx])
        if mem:
            mem["score"] = round(float(score), 4)
            results.append(mem)

    return results

def _normalize_subject(s: str) -> str:
    """Lower-case, strip, collapse whitespace — for subject comparison."""
    return " ".join(s.lower().split())


def find_by_subject(category: str | None, subject: str) -> dict | None:
    """Find an existing memory by normalised subject (and optionally category).

    This is a deterministic SQL lookup — no embedding similarity involved.

    Parameters
    ----------
    category : str or None
        If given, restricts the search to that category.
        If ``None``, searches across all categories.
    subject : str
        The subject to match (normalised: lowercase, stripped, collapsed
        whitespace).

    Returns the most recently updated match, or ``None``.
    """
    conn = _get_conn()
    if category is not None:
        cat = category.lower().strip()
        rows = conn.execute(
            "SELECT * FROM memories WHERE category = ? ORDER BY updated_at DESC",
            (cat,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY updated_at DESC",
        ).fetchall()
    conn.close()

    norm = _normalize_subject(subject)
    for row in rows:
        if _normalize_subject(row["subject"]) == norm:
            return dict(row)
    return None


def find_duplicate(
    category: str,
    subject: str,
    content: str,
    threshold: float = 0.92,
) -> dict | None:
    """Find an existing memory that is a near-duplicate.

    Returns the best match **only** if:
    1. Its semantic similarity score ≥ *threshold*, AND
    2. Its normalised subject matches the given subject.

    Returns ``None`` when no qualifying duplicate exists.
    """
    search_text = f"{category} {subject} {content}"
    try:
        results = semantic_search(search_text, top_k=5, threshold=threshold)
    except Exception:
        return None

    norm_subj = _normalize_subject(subject)
    for m in results:
        if _normalize_subject(m.get("subject", "")) == norm_subj:
            return m
    return None


def save_memory(
    category: str,
    subject: str,
    content: str,
    tags: str = "",
    source: str = "live",
) -> dict:
    """Create a new memory entry.

    Parameters
    ----------
    category : str
        One of the ``VALID_CATEGORIES``.
    subject : str
        Short identifier, e.g. a person's name or topic.
    content : str
        Free-text detail about the memory.
    tags : str
        Optional comma-separated tags for search.
    source : str
        Origin: ``'live'`` (agent during chat) or ``'extraction'``
        (background extractor).  Stored for diagnostics.

    Returns
    -------
    dict  with keys ``id``, ``category``, ``subject``, ``content``,
    ``tags``, ``source``, ``created_at``, ``updated_at``.
    """
    category = category.lower().strip()
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}"
        )

    mem_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO memories (id, category, subject, content, tags, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (mem_id, category, subject.strip(), content.strip(), tags.strip(),
         source.strip(), now, now),
    )
    conn.commit()
    conn.close()
    _rebuild_memory_index()
    return {
        "id": mem_id,
        "category": category,
        "subject": subject.strip(),
        "content": content.strip(),
        "tags": tags.strip(),
        "source": source.strip(),
        "created_at": now,
        "updated_at": now,
    }


def update_memory(
    memory_id: str,
    content: str,
    *,
    subject: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    source: str | None = None,
) -> dict | None:
    """Update an existing memory's fields.

    Only ``content`` is required.  Pass *subject*, *tags*, *category*,
    or *source* to update those fields as well.

    Returns the updated record dict, or ``None`` if not found.
    """
    now = datetime.now().isoformat()
    fields = ["content = ?", "updated_at = ?"]
    params: list = [content.strip(), now]

    if subject is not None:
        fields.append("subject = ?")
        params.append(subject.strip())
    if tags is not None:
        fields.append("tags = ?")
        params.append(tags.strip())
    if category is not None:
        cat = category.lower().strip()
        if cat in VALID_CATEGORIES:
            fields.append("category = ?")
            params.append(cat)
    if source is not None:
        fields.append("source = ?")
        params.append(source.strip())

    params.append(memory_id)
    conn = _get_conn()
    cur = conn.execute(
        f"UPDATE memories SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        return None
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    conn.close()
    if row:
        _rebuild_memory_index()
    return dict(row) if row else None


def delete_memory(memory_id: str) -> bool:
    """Delete a memory by ID.  Returns ``True`` if a row was deleted."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    conn.close()
    deleted = cur.rowcount > 0
    if deleted:
        _rebuild_memory_index()
    return deleted


def search_memories(query: str, category: str | None = None, limit: int = 20) -> list[dict]:
    """Search memories by keyword across subject, content, and tags.

    Parameters
    ----------
    query : str
        Keyword to search for (case-insensitive LIKE match).
    category : str, optional
        Restrict search to a single category.
    limit : int
        Maximum number of results.

    Returns
    -------
    list[dict]
    """
    conn = _get_conn()
    sql = (
        "SELECT * FROM memories WHERE "
        "(subject LIKE ? OR content LIKE ? OR tags LIKE ?)"
    )
    params: list = [f"%{query}%", f"%{query}%", f"%{query}%"]

    if category:
        category = category.lower().strip()
        sql += " AND category = ?"
        params.append(category)

    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_memories(category: str | None = None, limit: int = 50) -> list[dict]:
    """List memories, optionally filtered by category.

    Returns
    -------
    list[dict]
    """
    conn = _get_conn()
    if category:
        category = category.lower().strip()
        rows = conn.execute(
            "SELECT * FROM memories WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
            (category, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_memory(memory_id: str) -> dict | None:
    """Fetch a single memory by ID."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def count_memories() -> int:
    """Return total number of stored memories."""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    conn.close()
    return count


def delete_all_memories() -> int:
    """Delete every memory.  Returns the number of rows deleted."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM memories")
    conn.commit()
    conn.close()
    count = cur.rowcount
    if count:
        _rebuild_memory_index()
    return count


def consolidate_duplicates(threshold: float = 0.90) -> int:
    """Scan all memories and merge near-duplicates.

    For each pair sharing the same normalised subject and a semantic
    similarity score ≥ *threshold*, the shorter/older entry is merged
    into the longer/newer one and then deleted.

    Returns the number of memories removed.
    """
    all_mems = list_memories(limit=100_000)
    if len(all_mems) < 2:
        return 0

    # Group by normalised subject
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in all_mems:
        key = _normalize_subject(m["subject"])
        groups[key].append(m)

    removed = 0
    for _subj, mems in groups.items():
        if len(mems) < 2:
            continue
        # Within this subject group, compare pairwise using semantic search
        kept_ids: set[str] = set()
        deleted_ids: set[str] = set()
        for i, m1 in enumerate(mems):
            if m1["id"] in deleted_ids:
                continue
            for m2 in mems[i + 1:]:
                if m2["id"] in deleted_ids:
                    continue
                # Quick semantic check
                text1 = f"{m1['category']} {m1['subject']} {m1['content']}"
                try:
                    hits = semantic_search(text1, top_k=5, threshold=threshold)
                except Exception:
                    continue
                hit_ids = {h["id"] for h in hits}
                if m2["id"] not in hit_ids:
                    continue
                # They are near-duplicates — keep the richer one
                keep, drop = (m1, m2) if len(m1["content"]) >= len(m2["content"]) else (m2, m1)
                # Merge tags
                merged_tags = ", ".join(
                    dict.fromkeys(
                        t.strip()
                        for t in (keep.get("tags", "") + "," + drop.get("tags", "")).split(",")
                        if t.strip()
                    )
                )
                update_memory(keep["id"], keep["content"], tags=merged_tags)
                delete_memory(drop["id"])
                deleted_ids.add(drop["id"])
                removed += 1
                logger.info(
                    "Consolidated duplicate: kept %s (%s), removed %s",
                    keep["id"], keep["subject"], drop["id"],
                )

    return removed
