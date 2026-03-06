"""Long-term memory persistence layer — SQLite CRUD for personal memories.

Stores facts, preferences, people, events, places, and projects that the
agent can recall across conversations.  Each memory has a category, subject,
free-text content, and optional comma-separated tags for flexible search.

Database lives at ``~/.thoth/memory.db`` (separate from threads).
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import uuid
from datetime import datetime

# ── Data directory ───────────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(_DATA_DIR / "memory.db")

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
    conn.commit()
    conn.close()


_init_db()


# ── CRUD operations ─────────────────────────────────────────────────────────

def save_memory(
    category: str,
    subject: str,
    content: str,
    tags: str = "",
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

    Returns
    -------
    dict  with keys ``id``, ``category``, ``subject``, ``content``,
    ``tags``, ``created_at``, ``updated_at``.
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
        "INSERT INTO memories (id, category, subject, content, tags, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mem_id, category, subject.strip(), content.strip(), tags.strip(), now, now),
    )
    conn.commit()
    conn.close()
    return {
        "id": mem_id,
        "category": category,
        "subject": subject.strip(),
        "content": content.strip(),
        "tags": tags.strip(),
        "created_at": now,
        "updated_at": now,
    }


def update_memory(memory_id: str, content: str) -> dict | None:
    """Update the content of an existing memory.

    Returns the updated record dict, or ``None`` if not found.
    """
    now = datetime.now().isoformat()
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
        (content.strip(), now, memory_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        return None
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_memory(memory_id: str) -> bool:
    """Delete a memory by ID.  Returns ``True`` if a row was deleted."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


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
    return cur.rowcount
