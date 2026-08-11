from langgraph.checkpoint.sqlite import SqliteSaver
import logging
import sqlite3
import uuid
import os
import pathlib
import json
import time
import gc
from datetime import datetime, timedelta

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.approval_policy import DEFAULT_APPROVAL_MODE, normalize_approval_mode

logger = logging.getLogger(__name__)

DATA_DIR = get_row_bot_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

_THREAD_UI_DIR = DATA_DIR / "thread_ui"
_THREAD_UI_DIR.mkdir(parents=True, exist_ok=True)

_MEDIA_DIR = DATA_DIR / "media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(DATA_DIR / "threads.db")

_THREAD_META_COLUMNS = {
    "model_override": "TEXT DEFAULT ''",
    "skills_override": "TEXT DEFAULT ''",
    "summary": "TEXT DEFAULT ''",
    "summary_msg_count": "INTEGER DEFAULT 0",
    "project_id": "TEXT DEFAULT ''",
    "thread_type": "TEXT DEFAULT ''",
    "developer_workspace_id": "TEXT DEFAULT ''",
    "project_workspace_id": "TEXT DEFAULT ''",
    "approval_mode": "TEXT DEFAULT ''",
    "name_source": "TEXT DEFAULT ''",
    "agent_profile_id": "TEXT DEFAULT ''",
    "agent_profile_slug": "TEXT DEFAULT ''",
    "pinned_at": "TEXT DEFAULT ''",
}

THREAD_NAME_SOURCE_AUTO = "auto"
THREAD_NAME_SOURCE_MANUAL = "manual"
_THREAD_NAME_SOURCES = {THREAD_NAME_SOURCE_AUTO, THREAD_NAME_SOURCE_MANUAL}
_THREAD_NAME_MAX_LENGTH = 120
_DESKTOP_THREAD_BADGE = "\U0001f4bb"
_MOBILE_THREAD_BADGE = "\U0001f4f1"
_DEFAULT_AUTO_NAME_PREFIXES = (
    "Thread ",
    f"{_DESKTOP_THREAD_BADGE} Thread ",
    f"{_MOBILE_THREAD_BADGE} Thread ",
)


def _init_thread_db(*, raise_on_error: bool = False):
    """Create and migrate the thread metadata table."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS thread_meta "
                "(thread_id TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT)"
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_meta)").fetchall()}
            for column, definition in _THREAD_META_COLUMNS.items():
                if column not in cols:
                    conn.execute(f"ALTER TABLE thread_meta ADD COLUMN {column} {definition}")
                    cols.add(column)
            if "project_workspace_id" in cols and "developer_workspace_id" in cols:
                conn.execute(
                    "UPDATE thread_meta SET project_workspace_id = developer_workspace_id "
                    "WHERE COALESCE(project_workspace_id, '') = '' "
                    "AND COALESCE(developer_workspace_id, '') != '' "
                    "AND COALESCE(thread_type, '') = 'code'"
                )
            conn.commit()
        logger.debug("Thread database initialised at %s", DB_PATH)
    except Exception:
        logger.error("Failed to initialise thread database at %s", DB_PATH, exc_info=True)
        if raise_on_error:
            raise


def _ensure_thread_db() -> None:
    _init_thread_db(raise_on_error=True)

def _list_threads(*, include_details: bool = False):
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    if include_details:
        rows = conn.execute(
            "SELECT thread_id, name, created_at, updated_at, COALESCE(model_override, ''), "
            "COALESCE(project_id, ''), COALESCE(thread_type, ''), "
            "COALESCE(developer_workspace_id, ''), COALESCE(approval_mode, ''), "
            "COALESCE(name_source, ''), COALESCE(agent_profile_id, ''), "
            "COALESCE(agent_profile_slug, ''), COALESCE(project_workspace_id, ''), "
            "COALESCE(pinned_at, '') "
            "FROM thread_meta ORDER BY updated_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT thread_id, name, created_at, updated_at, COALESCE(model_override, ''), "
            "COALESCE(project_id, '') "
            "FROM thread_meta ORDER BY updated_at DESC"
        ).fetchall()
    conn.close()
    return rows


def cleanup_old_checkpoints(
    *,
    keep_per_thread: int = 10,
    min_age_minutes: int = 30,
) -> dict[str, int]:
    """Prune redundant LangGraph checkpoints while preserving latest state."""
    if keep_per_thread < 1:
        keep_per_thread = 1
    _ensure_thread_db()
    cutoff = (datetime.now() - timedelta(minutes=min_age_minutes)).isoformat()
    skipped_threads = _checkpoint_cleanup_skip_threads(cutoff)
    stats = {"threads": 0, "checkpoints": 0, "writes": 0}
    with sqlite3.connect(DB_PATH) as cleanup_conn:
        tables = {
            row[0]
            for row in cleanup_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "checkpoints" not in tables:
            return stats
        rows = cleanup_conn.execute(
            "SELECT rowid, thread_id, checkpoint_ns, checkpoint_id "
            "FROM checkpoints ORDER BY thread_id, checkpoint_ns, rowid DESC"
        ).fetchall()
        seen: dict[tuple[str, str], int] = {}
        delete_rows: list[int] = []
        delete_keys: list[tuple[str, str, str]] = []
        touched_threads: set[str] = set()
        for rowid, thread_id, checkpoint_ns, checkpoint_id in rows:
            if not thread_id or thread_id in skipped_threads:
                continue
            key = (str(thread_id), str(checkpoint_ns or ""))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= keep_per_thread:
                continue
            delete_rows.append(int(rowid))
            delete_keys.append((str(thread_id), str(checkpoint_ns or ""), str(checkpoint_id)))
            touched_threads.add(str(thread_id))
        if delete_rows:
            cleanup_conn.executemany(
                "DELETE FROM checkpoints WHERE rowid = ?",
                [(rowid,) for rowid in delete_rows],
            )
            stats["checkpoints"] = len(delete_rows)
        if delete_keys and "writes" in tables:
            before = cleanup_conn.total_changes
            cleanup_conn.executemany(
                "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                delete_keys,
            )
            stats["writes"] = max(0, cleanup_conn.total_changes - before)
        cleanup_conn.commit()
        stats["threads"] = len(touched_threads)
        try:
            cleanup_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass
    if stats["checkpoints"] or stats["writes"]:
        logger.info(
            "Checkpoint cleanup pruned %d checkpoint(s), %d write(s) across %d thread(s)",
            stats["checkpoints"],
            stats["writes"],
            stats["threads"],
        )
    return stats


def _checkpoint_cleanup_skip_threads(cutoff_iso: str) -> set[str]:
    skipped: set[str] = set()
    try:
        with sqlite3.connect(DB_PATH) as cleanup_conn:
            for tid, updated in cleanup_conn.execute(
                "SELECT thread_id, COALESCE(updated_at, '') FROM thread_meta"
            ).fetchall():
                if updated and str(updated) >= cutoff_iso:
                    skipped.add(str(tid))
    except Exception:
        logger.debug("Checkpoint cleanup could not read recent thread metadata", exc_info=True)
    try:
        from row_bot.ui.state import _active_generations
        skipped.update(str(tid) for tid in _active_generations.keys())
    except Exception:
        pass
    try:
        from row_bot.tasks import get_running_tasks
        skipped.update(str(tid) for tid in get_running_tasks().keys())
    except Exception:
        pass
    try:
        from row_bot.memory_extraction import _active_lock, _active_threads
        with _active_lock:
            skipped.update(str(tid) for tid in _active_threads)
    except Exception:
        pass
    return skipped

def _set_thread_project_id(thread_id: str, project_id: str) -> None:
    """Link a thread to a designer project."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET project_id = ? WHERE thread_id = ?",
        (project_id, thread_id),
    )
    conn.commit()
    conn.close()


def _get_thread_project_id(thread_id: str) -> str:
    """Return the project_id for a thread (empty string if none)."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(project_id, '') FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _set_thread_type(thread_id: str, thread_type: str) -> None:
    """Set a high-level thread type such as ``code``."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET thread_type = ? WHERE thread_id = ?",
        (thread_type, thread_id),
    )
    conn.commit()
    conn.close()


def _get_thread_type(thread_id: str) -> str:
    """Return the stored thread type, or an empty string."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(thread_type, '') FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _set_thread_developer_workspace(thread_id: str, workspace_id: str) -> None:
    """Link a thread to a Developer workspace."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET developer_workspace_id = ? WHERE thread_id = ?",
        (workspace_id, thread_id),
    )
    conn.commit()
    conn.close()


def _set_thread_project_workspace(thread_id: str, workspace_id: str) -> None:
    """Link a Developer thread to its root project workspace."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET project_workspace_id = ? WHERE thread_id = ?",
        (workspace_id, thread_id),
    )
    conn.commit()
    conn.close()


def _get_thread_approval_mode_raw(thread_id: str) -> str:
    """Return the stored thread approval mode without applying defaults."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(approval_mode, '') FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return str(row[0] or "") if row else ""


def _get_thread_approval_mode(thread_id: str) -> str:
    """Return the shared approval mode for a thread."""
    raw = _get_thread_approval_mode_raw(thread_id)
    return normalize_approval_mode(raw, DEFAULT_APPROVAL_MODE)


def _set_thread_approval_mode(thread_id: str, mode: str) -> None:
    """Persist the shared approval mode for a thread."""
    _ensure_thread_db()
    normalized = normalize_approval_mode(mode, DEFAULT_APPROVAL_MODE)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET approval_mode = ? WHERE thread_id = ?",
        (normalized, thread_id),
    )
    conn.commit()
    conn.close()


def _get_thread_agent_profile(thread_id: str) -> dict[str, str]:
    """Return the explicit Agent Profile pointer for a thread, if any."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(agent_profile_id, ''), COALESCE(agent_profile_slug, '') "
        "FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    if not row:
        return {"id": "", "slug": ""}
    return {"id": str(row[0] or ""), "slug": str(row[1] or "")}


def _set_thread_agent_profile(thread_id: str, profile_id_or_slug: str) -> dict[str, str]:
    """Persist an explicit Agent Profile pointer for a thread."""
    _ensure_thread_db()
    from row_bot.agent_profiles import require_agent_profile

    profile = require_agent_profile(profile_id_or_slug, enabled_only=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET agent_profile_id = ?, agent_profile_slug = ? "
        "WHERE thread_id = ?",
        (profile["id"], profile["slug"], thread_id),
    )
    conn.commit()
    conn.close()
    return {"id": profile["id"], "slug": profile["slug"]}


def _clear_thread_agent_profile(thread_id: str) -> None:
    """Clear the explicit Agent Profile pointer for a thread."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET agent_profile_id = '', agent_profile_slug = '' "
        "WHERE thread_id = ?",
        (thread_id,),
    )
    conn.commit()
    conn.close()


def _get_thread_developer_workspace(thread_id: str) -> str:
    """Return the linked Developer workspace id, or an empty string."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(developer_workspace_id, '') FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _get_thread_project_workspace(thread_id: str) -> str:
    """Return the Developer project/root workspace id for a thread."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(project_workspace_id, ''), COALESCE(developer_workspace_id, '') "
        "FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    if not row:
        return ""
    return str(row[0] or row[1] or "")


def _thread_exists(thread_id: str) -> bool:
    """Return True if a thread_meta row exists for *thread_id*."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM thread_meta WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    conn.close()
    return row is not None


def _normalize_thread_name(name: str, *, fallback: str | None = None) -> str:
    normalized = " ".join(str(name or "").strip().split())
    if not normalized:
        if fallback is None:
            raise ValueError("Thread name cannot be empty.")
        normalized = fallback
    return normalized[:_THREAD_NAME_MAX_LENGTH].rstrip() or (fallback or "Untitled")


def _auto_thread_badge_for_name(name: str | None) -> str:
    value = str(name or "").strip()
    mobile_placeholder = f"{_MOBILE_THREAD_BADGE} Thread"
    if value == mobile_placeholder or value.startswith(f"{mobile_placeholder} "):
        return _MOBILE_THREAD_BADGE
    return _DESKTOP_THREAD_BADGE


def build_auto_thread_title(seed_text: str, *, current_name: str | None = None) -> str:
    """Build a generated thread title while preserving the placeholder surface badge."""
    badge = _auto_thread_badge_for_name(current_name)
    seed = _normalize_thread_name(seed_text, fallback="Thread")
    return _normalize_thread_name(
        f"{badge} {seed[:50]}",
        fallback=f"{badge} Thread",
    )


def _normalize_thread_name_source(source: str | None) -> str:
    value = str(source or "").strip().lower()
    return value if value in _THREAD_NAME_SOURCES else THREAD_NAME_SOURCE_AUTO


def create_thread(
    name: str,
    *,
    thread_id: str | None = None,
    thread_type: str = "",
    developer_workspace_id: str = "",
    project_workspace_id: str = "",
    project_id: str = "",
    approval_mode: str = "",
    model_override: str = "",
    agent_profile_id: str = "",
    agent_profile_slug: str = "",
    name_source: str = THREAD_NAME_SOURCE_AUTO,
    seed_default_skills: bool = True,
) -> str:
    """Create or replace the metadata row for a conversation thread."""
    _ensure_thread_db()
    tid = str(thread_id or uuid.uuid4().hex[:12])
    safe_name = _normalize_thread_name(name, fallback="Untitled")
    safe_source = _normalize_thread_name_source(name_source)
    safe_approval = (
        normalize_approval_mode(approval_mode, DEFAULT_APPROVAL_MODE)
        if str(approval_mode or "").strip()
        else ""
    )
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        existed = conn.execute(
            "SELECT 1 FROM thread_meta WHERE thread_id = ?",
            (tid,),
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO thread_meta ("
            "thread_id, name, created_at, updated_at, model_override, project_id, "
            "thread_type, developer_workspace_id, project_workspace_id, approval_mode, name_source, "
            "agent_profile_id, agent_profile_slug"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "name = excluded.name, updated_at = excluded.updated_at, "
            "model_override = excluded.model_override, project_id = excluded.project_id, "
            "thread_type = excluded.thread_type, "
            "developer_workspace_id = excluded.developer_workspace_id, "
            "project_workspace_id = excluded.project_workspace_id, "
            "approval_mode = excluded.approval_mode, name_source = excluded.name_source, "
            "agent_profile_id = excluded.agent_profile_id, "
            "agent_profile_slug = excluded.agent_profile_slug",
            (
                tid,
                safe_name,
                now,
                now,
                str(model_override or ""),
                str(project_id or ""),
                str(thread_type or ""),
                str(developer_workspace_id or ""),
                str(project_workspace_id or developer_workspace_id or ""),
                safe_approval,
                safe_source,
                str(agent_profile_id or ""),
                str(agent_profile_slug or ""),
            ),
        )
        conn.commit()
    if (
        seed_default_skills
        and not existed
        and not str(project_id or "").strip()
        and not str(developer_workspace_id or "").strip()
        and not str(project_workspace_id or "").strip()
        and not str(thread_type or "").strip()
    ):
        _seed_thread_default_skills_safe(tid, surface="chat")
    return tid


def rename_thread(
    thread_id: str,
    name: str,
    *,
    source: str = THREAD_NAME_SOURCE_MANUAL,
) -> str:
    """Rename a thread and mark whether the title is manual or generated."""
    _ensure_thread_db()
    tid = str(thread_id or "").strip()
    if not tid:
        raise ValueError("Thread id cannot be empty.")
    safe_name = _normalize_thread_name(name)
    safe_source = _normalize_thread_name_source(source)
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO thread_meta (thread_id, name, created_at, updated_at, name_source) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET name = ?, updated_at = ?, name_source = ?",
            (tid, safe_name, now, now, safe_source, safe_name, now, safe_source),
        )
        conn.commit()
    return safe_name


def get_thread_name(thread_id: str) -> str:
    """Return the stored display name for a thread."""
    _ensure_thread_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(name, '') FROM thread_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return str(row[0] or "") if row else ""


def touch_thread(thread_id: str) -> None:
    """Bump a thread's recency without changing its title."""
    if not thread_id:
        return
    _ensure_thread_db()
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE thread_meta SET updated_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )
        conn.commit()


def set_thread_pinned(thread_id: str, pinned: bool) -> str:
    """Set a thread's pin state without changing its recency timestamp."""

    _ensure_thread_db()
    tid = str(thread_id or "").strip()
    if not tid:
        raise ValueError("Thread id cannot be empty.")
    pinned_at = datetime.now().isoformat() if pinned else ""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "UPDATE thread_meta SET pinned_at = ? WHERE thread_id = ?",
            (pinned_at, tid),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Thread not found: {tid}")
        conn.commit()
    return pinned_at


def pin_thread(thread_id: str) -> str:
    """Pin a thread and return the stored pin timestamp."""

    return set_thread_pinned(thread_id, True)


def unpin_thread(thread_id: str) -> None:
    """Clear a thread's pin state."""

    set_thread_pinned(thread_id, False)


def is_thread_pinned(thread_id: str) -> bool:
    """Return True when a thread has a non-empty pin timestamp."""

    _ensure_thread_db()
    tid = str(thread_id or "").strip()
    if not tid:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(pinned_at, '') FROM thread_meta WHERE thread_id = ?",
            (tid,),
        ).fetchone()
    return bool(str(row[0] or "").strip()) if row else False


def get_thread_name_source(thread_id: str) -> str:
    """Return ``auto``, ``manual``, or an empty legacy source marker."""
    _ensure_thread_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(name_source, '') FROM thread_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return str(row[0] or "") if row else ""


def _looks_like_auto_thread_name(name: str | None) -> bool:
    value = str(name or "").strip()
    if not value:
        return True
    if value in {
        "Thread",
        f"{_DESKTOP_THREAD_BADGE} Thread",
        f"{_MOBILE_THREAD_BADGE} Thread",
    }:
        return True
    return any(value.startswith(prefix) for prefix in _DEFAULT_AUTO_NAME_PREFIXES)


def should_auto_rename_thread(thread_id: str, current_name: str | None = None) -> bool:
    """Return True when generated-title logic may still replace this title."""
    name = current_name if current_name is not None else get_thread_name(thread_id)
    if get_thread_name_source(thread_id) == THREAD_NAME_SOURCE_MANUAL:
        return False
    return _looks_like_auto_thread_name(name)


def list_developer_workspace_threads(workspace_id: str) -> list[tuple]:
    """Return all thread metadata rows linked to a Developer workspace."""
    _ensure_thread_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT thread_id, name, created_at, updated_at, COALESCE(model_override, ''), "
            "COALESCE(project_id, ''), COALESCE(thread_type, ''), "
            "COALESCE(developer_workspace_id, ''), COALESCE(approval_mode, ''), "
            "COALESCE(name_source, ''), COALESCE(project_workspace_id, ''), "
            "COALESCE(pinned_at, '') "
            "FROM thread_meta WHERE COALESCE(thread_type, '') = 'code' "
            "AND (COALESCE(project_workspace_id, '') = ? OR "
            "(COALESCE(project_workspace_id, '') = '' AND COALESCE(developer_workspace_id, '') = ?)) "
            "ORDER BY updated_at DESC",
            (workspace_id, workspace_id),
        ).fetchall()
    return rows


def _seed_thread_default_skills_safe(thread_id: str, *, surface: str = "chat") -> None:
    try:
        from row_bot.skills_activation import seed_thread_default_skills

        seed_thread_default_skills(thread_id, surface=surface)
    except Exception:
        logger.debug(
            "Failed to seed default skills for thread %s",
            thread_id,
            exc_info=True,
        )


def _save_thread_meta(thread_id: str, name: str, *, seed_default_skills: bool = False):
    _ensure_thread_db()
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    existed = conn.execute(
        "SELECT 1 FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone() is not None
    conn.execute(
        "INSERT INTO thread_meta (thread_id, name, created_at, updated_at, name_source) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(thread_id) DO UPDATE SET name = ?, updated_at = ?",
        (thread_id, name, now, now, THREAD_NAME_SOURCE_AUTO, name, now),
    )
    conn.commit()
    conn.close()
    if seed_default_skills and not existed:
        _seed_thread_default_skills_safe(thread_id, surface="chat")


def _thread_ui_media_path(thread_id: str) -> pathlib.Path:
    return _THREAD_UI_DIR / f"{thread_id}.media.json"


def _thread_ui_draft_path(thread_id: str) -> pathlib.Path:
    safe_id = str(thread_id or "").strip()
    return _THREAD_UI_DIR / f"{safe_id}.draft.json"


def save_thread_draft(thread_id: str, text: str, *, source: str = "") -> None:
    """Persist a composer draft for a thread until it is sent or replaced."""

    if not thread_id:
        return
    try:
        payload = {
            "thread_id": str(thread_id),
            "text": str(text or ""),
            "source": str(source or ""),
            "updated_at": datetime.now().isoformat(),
        }
        _thread_ui_draft_path(thread_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        logger.warning("Failed to save thread draft for %s", thread_id, exc_info=True)


def load_thread_draft(thread_id: str) -> dict | None:
    """Load a persisted composer draft for a thread, if present."""

    if not thread_id:
        return None
    try:
        path = _thread_ui_draft_path(thread_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        logger.warning("Failed to load thread draft for %s", thread_id, exc_info=True)
        return None


def delete_thread_draft(thread_id: str) -> None:
    """Remove a persisted composer draft."""

    if not thread_id:
        return
    try:
        _thread_ui_draft_path(thread_id).unlink(missing_ok=True)
    except Exception:
        logger.debug("Failed to delete thread draft for %s", thread_id, exc_info=True)


def _thread_media_dir(thread_id: str) -> pathlib.Path:
    """Return (and lazily create) the per-thread media directory."""
    d = _MEDIA_DIR / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_thread_media(thread_id: str, payload: dict) -> None:
    """Persist media sidecar (v2 — file paths, not base64)."""
    try:
        path = _thread_ui_media_path(thread_id)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("Failed to save thread media sidecar for %s", thread_id, exc_info=True)


def load_thread_media(thread_id: str) -> dict | None:
    """Load media sidecar for a thread (if any)."""
    try:
        path = _thread_ui_media_path(thread_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("Failed to load thread media sidecar for %s", thread_id, exc_info=True)
        return None


def save_media_file(thread_id: str, filename: str, data: bytes) -> pathlib.Path:
    """Write raw media bytes to the per-thread media directory.

    Returns the absolute path to the saved file.
    """
    d = _thread_media_dir(thread_id)
    dest = d / filename
    dest.write_bytes(data)
    return dest


def load_media_file(thread_id: str, filename: str) -> bytes | None:
    """Read a media file from the per-thread media directory."""
    path = _MEDIA_DIR / thread_id / filename
    if path.exists():
        try:
            return path.read_bytes()
        except Exception:
            logger.warning("Failed to read media file %s", path, exc_info=True)
    return None


def _next_media_filename(thread_id: str, prefix: str, ext: str) -> str:
    """Generate the next sequential filename like gen_001.png, cap_002.png."""
    d = _MEDIA_DIR / thread_id
    if not d.exists():
        return f"{prefix}_001.{ext}"
    existing = [f.name for f in d.iterdir() if f.name.startswith(prefix + "_")]
    if not existing:
        return f"{prefix}_001.{ext}"
    nums = []
    for name in existing:
        parts = name.split("_", 1)
        if len(parts) == 2:
            num_part = parts[1].split(".")[0]
            try:
                nums.append(int(num_part))
            except ValueError:
                pass
    next_num = max(nums, default=0) + 1
    return f"{prefix}_{next_num:03d}.{ext}"

_init_thread_db()

def _delete_thread(thread_id: str):
    """Remove a thread's metadata, checkpoints, and writes from the database."""
    _ensure_thread_db()
    try:
        from row_bot.computer_use.service import get_computer_use_service

        get_computer_use_service().close_for_thread(thread_id)
    except Exception:
        logger.warning("Failed to clean up Computer Use state for thread %s", thread_id, exc_info=True)
    try:
        from row_bot.agent_runs import cleanup_thread_agent_runs

        cleanup_thread_agent_runs(thread_id)
    except Exception:
        logger.warning("Failed to clean up Agent Run state for thread %s", thread_id, exc_info=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM thread_meta WHERE thread_id = ?", (thread_id,))
    # Purge LangGraph checkpoint data to prevent zombie threads
    # Tables are created by LangGraph at runtime — may not exist yet
    try:
        conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    # Clear any cached summary for this thread
    try:
        from row_bot.agent import clear_summary_cache
        clear_summary_cache(thread_id)
    except Exception:
        pass
    # Clean up media sidecar and non-persistent media files
    try:
        sidecar = _thread_ui_media_path(thread_id)
        media_dir = _MEDIA_DIR / thread_id
        # Read sidecar to find which files to keep (persist=true)
        persist_files: set[str] = set()
        if sidecar.exists():
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                for entry in payload.get("entries", []):
                    for item in entry.get("media", []):
                        if item.get("persist"):
                            persist_files.add(item.get("path", ""))
            except Exception:
                logger.debug("Failed to parse media sidecar during delete", exc_info=True)
            sidecar.unlink(missing_ok=True)
        # Delete non-persistent files; leave persistent ones
        if media_dir.exists():
            for f in list(media_dir.iterdir()):
                if f.name not in persist_files:
                    try:
                        f.unlink()
                    except Exception:
                        logger.debug("Failed to delete media file %s", f, exc_info=True)
            # Remove dir only if empty
            try:
                if not any(media_dir.iterdir()):
                    media_dir.rmdir()
            except Exception:
                pass
    except Exception:
        logger.warning("Failed to clean up media for thread %s", thread_id, exc_info=True)
    # Also clean up legacy sidecar if present
    try:
        legacy = _THREAD_UI_DIR / f"{thread_id}.images.json"
        legacy.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        delete_thread_draft(thread_id)
    except Exception:
        pass
    try:
        from row_bot.skills_activation import delete_thread_activation_state

        delete_thread_activation_state(thread_id)
    except Exception:
        logger.debug("Failed to clean up skill activation state for thread %s", thread_id, exc_info=True)


def delete_threads(thread_ids: list[str]) -> tuple[int, list[tuple[str, str]]]:
    """Delete several threads at once.

    Loops over :func:`_delete_thread` so all existing side effects
    (checkpoint purge, media cleanup, summary cache invalidation) are
    preserved per thread. Returns ``(deleted_count, failures)`` where
    ``failures`` is a list of ``(thread_id, error_message)``.

    The UI layer is responsible for additional cleanup that lives
    outside this module (shell/browser session kills, active-generation
    stops, state invalidation) — this helper only touches the same
    surfaces that :func:`_delete_thread` does.
    """
    deleted = 0
    failures: list[tuple[str, str]] = []
    for tid in thread_ids:
        try:
            _delete_thread(tid)
            deleted += 1
        except Exception as exc:  # pragma: no cover — defensive
            failures.append((tid, str(exc)))
            logger.exception("Bulk delete failed for thread %s", tid)
    return deleted, failures


def purge_external_state(thread_id: str) -> None:
    """Best-effort cleanup of state that lives outside threads.py.

    Covers: active-generation stop, task-run stop, agent summary cache,
    shell/browser tool sessions + histories. Every step is guarded so a
    partial environment (e.g. tests without tools loaded) won't crash.
    Safe to call before or after :func:`_delete_thread`.
    """
    if not thread_id:
        return
    # Active generation
    try:
        from row_bot.ui.state import _active_generations  # lazy import
        gen = _active_generations.get(thread_id)
        if gen:
            try:
                gen.stop_event.set()
            except Exception:
                pass
    except Exception:
        pass
    # Background task run
    try:
        from row_bot.tasks import stop_task
        stop_task(thread_id)
    except Exception:
        pass
    # Agent summary cache
    try:
        from row_bot.agent import clear_summary_cache
        clear_summary_cache(thread_id)
    except Exception:
        pass
    # Shell tool
    try:
        from row_bot.tools.shell_tool import get_session_manager, clear_shell_history
        get_session_manager().kill_session(thread_id)
        clear_shell_history(thread_id)
    except Exception:
        pass
    # Browser tool
    try:
        from row_bot.tools.browser_tool import (
            get_session_manager as get_browser_session_manager,
            clear_browser_history,
        )
        get_browser_session_manager().kill_session(thread_id)
        clear_browser_history(thread_id)
    except Exception:
        pass


def get_workflow_thread_ids() -> set[str]:
    """Return the set of thread_ids that belong to a workflow/task.

    Union of ``task_runs.thread_id`` and ``tasks.persistent_thread_id``.
    Used by the sidebar filter to classify threads as workflow runs so
    they can be filtered / badged distinctly from regular chats.
    """
    ids: set[str] = set()
    try:
        from row_bot.tasks import _get_conn  # lazy import to avoid cycles
        conn = _get_conn()
        try:
            for (tid,) in conn.execute(
                "SELECT DISTINCT thread_id FROM task_runs "
                "WHERE thread_id IS NOT NULL AND thread_id != ''"
            ):
                ids.add(tid)
            for (tid,) in conn.execute(
                "SELECT persistent_thread_id FROM tasks "
                "WHERE persistent_thread_id IS NOT NULL AND persistent_thread_id != ''"
            ):
                ids.add(tid)
        finally:
            conn.close()
    except Exception:
        logger.debug("Failed to read workflow thread ids", exc_info=True)
    return ids


def classify_thread(
    project_id: str,
    thread_id: str,
    workflow_tids: set[str] | None = None,
    thread_type: str = "",
    developer_workspace_id: str = "",
) -> str:
    """Return ``"designer"``, ``"code"``, ``"workflow"``, or ``"chat"``.

    Designer takes precedence over workflow (a thread shouldn't carry
    both, but if it does, the project view is the richer home).
    """
    if project_id:
        return "designer"
    if thread_type == "code" or developer_workspace_id:
        return "code"
    if workflow_tids is None:
        workflow_tids = get_workflow_thread_ids()
    if thread_id in workflow_tids:
        return "workflow"
    return "chat"


def sweep_orphan_project_ids() -> int:
    """Startup helper: fully purge thread_meta rows whose referenced
    designer project JSON is missing.

    Previous versions only cleared the ``project_id`` column so rows
    would fall into the generic "chat" bucket, but that leaves zombie
    conversations that the user can no longer meaningfully open.
    We now delete the row and its LangGraph data via
    :func:`_delete_thread` so the sidebar stays clean.

    Returns the number of threads deleted.
    """
    try:
        from row_bot.designer.storage import PROJECTS_DIR
    except Exception:
        return 0
    removed = 0
    try:
        _ensure_thread_db()
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT thread_id, COALESCE(project_id, '') FROM thread_meta "
            "WHERE COALESCE(project_id, '') != ''"
        ).fetchall()
        conn.close()
        orphans = [tid for tid, pid in rows
                   if not (PROJECTS_DIR / f"{pid}.json").exists()]
        for tid in orphans:
            try:
                purge_external_state(tid)
                _delete_thread(tid)
                removed += 1
            except Exception:
                logger.exception("Failed to purge orphan thread %s", tid)
        if removed:
            logger.info("Orphan project sweep removed %d thread(s)", removed)
    except Exception:
        logger.exception("sweep_orphan_project_ids failed")
    return removed


def _get_thread_model_override(thread_id: str) -> str:
    """Return the model override for a thread (empty string if none)."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(model_override, '') FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _set_thread_model_override(thread_id: str, model_name: str) -> None:
    """Set or clear the model override for a thread."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET model_override = ? WHERE thread_id = ?",
        (model_name, thread_id),
    )
    conn.commit()
    conn.close()


def get_thread_skills_override(thread_id: str) -> list[str] | None:
    """Return per-thread skills override as a list of skill names, or None (use global)."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(skills_override, '') FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    import json
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def set_thread_skills_override(thread_id: str, skill_names: list[str] | None) -> None:
    """Set or clear the per-thread skills override. Pass None to revert to global."""
    _ensure_thread_db()
    import json
    value = json.dumps(skill_names) if skill_names is not None else ""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET skills_override = ? WHERE thread_id = ?",
        (value, thread_id),
    )
    conn.commit()
    conn.close()


def save_thread_summary(thread_id: str, summary: str, msg_count: int) -> None:
    """Persist the context summary for a thread to the database."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET summary = ?, summary_msg_count = ? WHERE thread_id = ?",
        (summary, msg_count, thread_id),
    )
    conn.commit()
    conn.close()


def load_thread_summary(thread_id: str) -> dict | None:
    """Load the persisted summary for a thread, or None if none exists."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(summary, ''), COALESCE(summary_msg_count, 0) "
        "FROM thread_meta WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    return {"summary": row[0], "msg_count": row[1]}


def clear_thread_summary(thread_id: str) -> None:
    """Clear the persisted summary for a thread."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE thread_meta SET summary = '', summary_msg_count = 0 WHERE thread_id = ?",
        (thread_id,),
    )
    conn.commit()
    conn.close()


class _ManagedSqliteConnection:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._closed = False

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._conn.close()
        gc.collect()


conn = _ManagedSqliteConnection(DB_PATH)
checkpointer = SqliteSaver(conn)


def _version_to_int(value) -> int:
    try:
        if isinstance(value, int):
            return value
        text = str(value or "")
        return int(text.split(".", 1)[0]) if text else 0
    except (TypeError, ValueError):
        return 0


def _normalize_checkpoint_version(value):
    if isinstance(value, int):
        return f"{value:032}.0000000000000000"
    if isinstance(value, str) and value.isdigit():
        return f"{int(value):032}.0000000000000000"
    return value


def _normalize_checkpoint_versions(checkpoint: dict | None) -> tuple[dict | None, bool]:
    if not isinstance(checkpoint, dict):
        return checkpoint, False
    changed = False
    normalized = dict(checkpoint)
    versions = dict(normalized.get("channel_versions") or {})
    normalized_versions = {}
    for key, value in versions.items():
        next_value = _normalize_checkpoint_version(value)
        changed = changed or next_value != value
        normalized_versions[key] = next_value
    normalized["channel_versions"] = normalized_versions

    versions_seen = {}
    for node, seen in dict(normalized.get("versions_seen") or {}).items():
        if not isinstance(seen, dict):
            versions_seen[node] = seen
            continue
        next_seen = {}
        for key, value in seen.items():
            next_value = _normalize_checkpoint_version(value)
            changed = changed or next_value != value
            next_seen[key] = next_value
        versions_seen[node] = next_seen
    normalized["versions_seen"] = versions_seen
    return normalized, changed


def repair_thread_checkpoint_versions(thread_id: str) -> bool:
    """Append a normalized checkpoint if the latest one has legacy int versions."""
    if not thread_id:
        return False
    try:
        from langgraph.checkpoint.base import empty_checkpoint

        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
        checkpoint_tuple = checkpointer.get_tuple(config)
        checkpoint = getattr(checkpoint_tuple, "checkpoint", None) if checkpoint_tuple else None
        normalized, changed = _normalize_checkpoint_versions(checkpoint)
        if not changed or not isinstance(normalized, dict):
            return False
        next_checkpoint = empty_checkpoint()
        next_checkpoint["channel_values"] = dict(normalized.get("channel_values", {}))
        next_checkpoint["channel_versions"] = dict(normalized.get("channel_versions", {}))
        next_checkpoint["versions_seen"] = dict(normalized.get("versions_seen", {}))
        next_checkpoint["pending_sends"] = list(normalized.get("pending_sends", []))
        put_config = getattr(checkpoint_tuple, "config", None) or config
        put_config.setdefault("configurable", {})
        put_config["configurable"].setdefault("thread_id", str(thread_id))
        put_config["configurable"].setdefault("checkpoint_ns", "")
        metadata = dict(getattr(checkpoint_tuple, "metadata", None) or {})
        metadata["source"] = metadata.get("source") or "checkpoint_repair"
        metadata["writes"] = metadata.get("writes") or {}
        checkpointer.put(put_config, next_checkpoint, metadata, {})
        logger.info("Repaired checkpoint channel version types for thread %s", str(thread_id)[:8])
        return True
    except Exception:
        logger.warning("Failed to repair checkpoint channel versions for thread %s", thread_id, exc_info=True)
        return False


def get_latest_checkpoint_messages(thread_id: str) -> list:
    """Return raw LangGraph messages for a thread without building the agent graph."""
    if not thread_id:
        return []
    started = time.perf_counter()
    config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
    try:
        checkpoint_tuple = checkpointer.get_tuple(config)
        checkpoint = getattr(checkpoint_tuple, "checkpoint", None) if checkpoint_tuple else None
        channel_values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
        messages = channel_values.get("messages", [])
        if isinstance(messages, list):
            logger.debug(
                "perf: checkpoint messages read in %.3fs thread=%s count=%d",
                time.perf_counter() - started,
                str(thread_id)[:8],
                len(messages),
            )
            return list(messages)
    except Exception:
        logger.debug("Failed to read checkpoint messages for thread %s", thread_id, exc_info=True)
    logger.debug(
        "perf: checkpoint messages read in %.3fs thread=%s count=0",
        time.perf_counter() - started,
        str(thread_id)[:8],
    )
    return []


def get_latest_checkpoint_revision(thread_id: str) -> str:
    """Return a stable identity for the latest parent-thread checkpoint."""

    if not thread_id:
        return ""
    config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
    try:
        checkpoint_tuple = checkpointer.get_tuple(config)
        if not checkpoint_tuple:
            return ""
        tuple_config = getattr(checkpoint_tuple, "config", None) or {}
        configurable = (
            tuple_config.get("configurable", {})
            if isinstance(tuple_config, dict)
            else {}
        )
        checkpoint_id = str(configurable.get("checkpoint_id") or "").strip()
        if checkpoint_id:
            return checkpoint_id
        checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
        if isinstance(checkpoint, dict):
            fallback_id = str(checkpoint.get("id") or "").strip()
            if fallback_id:
                return fallback_id
            message_version = (checkpoint.get("channel_versions") or {}).get(
                "messages"
            )
            if message_version is not None:
                return str(message_version)
    except Exception:
        logger.debug(
            "Failed to read checkpoint revision for thread %s",
            thread_id,
            exc_info=True,
        )
    return ""


def append_checkpoint_messages(thread_id: str, messages: list) -> bool:
    """Append simple chat messages to checkpoint storage without constructing a graph."""
    if not thread_id or not messages:
        return False
    try:
        from langgraph.checkpoint.base import empty_checkpoint

        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
        checkpoint_tuple = checkpointer.get_tuple(config)
        parent_config = getattr(checkpoint_tuple, "config", None) if checkpoint_tuple else None
        checkpoint = getattr(checkpoint_tuple, "checkpoint", None) if checkpoint_tuple else None
        checkpoint, _changed = _normalize_checkpoint_versions(checkpoint)
        channel_values = dict(checkpoint.get("channel_values", {})) if isinstance(checkpoint, dict) else {}
        existing = channel_values.get("messages", [])
        if not isinstance(existing, list):
            existing = []
        channel_values["messages"] = [*existing, *messages]

        next_checkpoint = empty_checkpoint()
        next_checkpoint["channel_values"] = channel_values
        channel_versions = dict(checkpoint.get("channel_versions", {})) if isinstance(checkpoint, dict) else {}
        current_version = channel_versions.get("messages")
        next_version = checkpointer.get_next_version(current_version, None)
        channel_versions["messages"] = next_version
        next_checkpoint["channel_versions"] = channel_versions
        next_checkpoint["versions_seen"] = dict(checkpoint.get("versions_seen", {})) if isinstance(checkpoint, dict) else {}

        put_config = parent_config or config
        put_config.setdefault("configurable", {})
        put_config["configurable"].setdefault("thread_id", str(thread_id))
        put_config["configurable"].setdefault("checkpoint_ns", "")
        checkpointer.put(
            put_config,
            next_checkpoint,
            {"source": "chat_only", "step": _version_to_int(next_version), "writes": {"messages": len(messages)}},
            {"messages": next_version},
        )
        logger.debug("Appended %d checkpoint message(s) for thread %s", len(messages), str(thread_id)[:8])
        return True
    except Exception:
        logger.warning("Failed to append checkpoint messages for thread %s", thread_id, exc_info=True)
        return False


def remove_latest_checkpoint_ai_message(thread_id: str, expected_text: str) -> bool:
    """Remove the provisional assistant/tool suffix of the latest user turn.

    Durable orchestration uses this before appending its acknowledgement. The
    exact-content check on the latest AI answer prevents an older or concurrent
    turn from being removed. Removing the whole suffix also keeps tool-call
    drafts and raw Agent tool output out of the final parent transcript.
    """

    if not thread_id or not str(expected_text or "").strip():
        return False
    try:
        from langgraph.checkpoint.base import empty_checkpoint

        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
        checkpoint_tuple = checkpointer.get_tuple(config)
        parent_config = getattr(checkpoint_tuple, "config", None) if checkpoint_tuple else None
        checkpoint = getattr(checkpoint_tuple, "checkpoint", None) if checkpoint_tuple else None
        checkpoint, _changed = _normalize_checkpoint_versions(checkpoint)
        channel_values = dict(checkpoint.get("channel_values", {})) if isinstance(checkpoint, dict) else {}
        existing = channel_values.get("messages", [])
        if not isinstance(existing, list):
            return False
        remove_index = -1
        for index in range(len(existing) - 1, -1, -1):
            message = existing[index]
            if str(getattr(message, "type", "") or "") != "ai":
                continue
            content = getattr(message, "content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(item.get("text") or item.get("content") or "")
                    if isinstance(item, dict)
                    else str(item)
                    for item in content
                )
            actual_text = str(content or "").strip()
            expected = str(expected_text or "").strip()
            if not actual_text:
                continue
            if actual_text != expected and not expected.endswith(actual_text):
                return False
            remove_index = index
            break
        if remove_index < 0:
            return False
        latest_human_index = -1
        for index in range(remove_index - 1, -1, -1):
            if str(getattr(existing[index], "type", "") or "") == "human":
                latest_human_index = index
                break
        if latest_human_index < 0:
            return False
        removed_count = len(existing) - latest_human_index - 1
        channel_values["messages"] = existing[: latest_human_index + 1]

        next_checkpoint = empty_checkpoint()
        next_checkpoint["channel_values"] = channel_values
        channel_versions = dict(checkpoint.get("channel_versions", {})) if isinstance(checkpoint, dict) else {}
        current_version = channel_versions.get("messages")
        next_version = checkpointer.get_next_version(current_version, None)
        channel_versions["messages"] = next_version
        next_checkpoint["channel_versions"] = channel_versions
        next_checkpoint["versions_seen"] = dict(checkpoint.get("versions_seen", {})) if isinstance(checkpoint, dict) else {}
        next_checkpoint["pending_sends"] = list(checkpoint.get("pending_sends", [])) if isinstance(checkpoint, dict) else []

        put_config = parent_config or config
        put_config.setdefault("configurable", {})
        put_config["configurable"].setdefault("thread_id", str(thread_id))
        put_config["configurable"].setdefault("checkpoint_ns", "")
        checkpointer.put(
            put_config,
            next_checkpoint,
            {
                "source": "orchestration_suspend",
                "step": _version_to_int(next_version),
                "writes": {"messages": -removed_count},
            },
            {"messages": next_version},
        )
        return True
    except Exception:
        logger.warning(
            "Failed to remove provisional orchestration answer for thread %s",
            thread_id,
            exc_info=True,
        )
        return False


def pick_or_create_thread() -> dict:
    """Interactive menu to resume an existing thread or start a new one."""
    threads = _list_threads()
    print("\n=== Row-Bot — Thread Manager ===")
    print("  [0] Start a new conversation")
    for idx, (tid, name, created, updated, *_pick_rest) in enumerate(threads, start=1):
        print(f"  [{idx}] {name}  (last used: {updated[:16]})")
    print()

    while True:
        choice = input("Select a thread number: ").strip()
        if choice == "0":
            thread_id = uuid.uuid4().hex[:12]
            name = input("Give this conversation a name: ").strip() or f"Thread-{thread_id[:6]}"
            _save_thread_meta(thread_id, name, seed_default_skills=True)
            print(f"\nStarted new thread: {name}\n")
            return {"configurable": {"thread_id": thread_id}}
        elif choice.isdigit() and 1 <= int(choice) <= len(threads):
            tid, name, _, _, *_pick_rest2 = threads[int(choice) - 1]
            _save_thread_meta(tid, name)  # bump updated_at
            print(f"\nResuming thread: {name}\n")
            return {"configurable": {"thread_id": tid}}
        else:
            print("Invalid choice, try again.")
