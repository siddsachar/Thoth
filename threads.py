from langgraph.checkpoint.sqlite import SqliteSaver
import logging
import sqlite3
import uuid
import os
import pathlib
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Store data in %APPDATA%/Thoth (writable even when app is in Program Files)
DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
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
}


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
            "COALESCE(developer_workspace_id, '') "
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
        from ui.state import _active_generations
        skipped.update(str(tid) for tid in _active_generations.keys())
    except Exception:
        pass
    try:
        from tasks import get_running_tasks
        skipped.update(str(tid) for tid in get_running_tasks().keys())
    except Exception:
        pass
    try:
        from memory_extraction import _active_lock, _active_threads
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


def _thread_exists(thread_id: str) -> bool:
    """Return True if a thread_meta row exists for *thread_id*."""
    _ensure_thread_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM thread_meta WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    conn.close()
    return row is not None

def _save_thread_meta(thread_id: str, name: str):
    _ensure_thread_db()
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO thread_meta (thread_id, name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(thread_id) DO UPDATE SET name = ?, updated_at = ?",
        (thread_id, name, now, now, name, now),
    )
    conn.commit()
    conn.close()


def _thread_ui_media_path(thread_id: str) -> pathlib.Path:
    return _THREAD_UI_DIR / f"{thread_id}.media.json"


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
        from agent import clear_summary_cache
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
        from ui.state import _active_generations  # lazy import
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
        from tasks import stop_task
        stop_task(thread_id)
    except Exception:
        pass
    # Agent summary cache
    try:
        from agent import clear_summary_cache
        clear_summary_cache(thread_id)
    except Exception:
        pass
    # Shell tool
    try:
        from tools.shell_tool import get_session_manager, clear_shell_history
        get_session_manager().kill_session(thread_id)
        clear_shell_history(thread_id)
    except Exception:
        pass
    # Browser tool
    try:
        from tools.browser_tool import (
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
        from tasks import _get_conn  # lazy import to avoid cycles
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
        from designer.storage import PROJECTS_DIR
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


conn = sqlite3.connect(DB_PATH, check_same_thread=False)
checkpointer = SqliteSaver(conn)


def pick_or_create_thread() -> dict:
    """Interactive menu to resume an existing thread or start a new one."""
    threads = _list_threads()
    print("\n=== Thoth — Thread Manager ===")
    print("  [0] Start a new conversation")
    for idx, (tid, name, created, updated, *_pick_rest) in enumerate(threads, start=1):
        print(f"  [{idx}] {name}  (last used: {updated[:16]})")
    print()

    while True:
        choice = input("Select a thread number: ").strip()
        if choice == "0":
            thread_id = uuid.uuid4().hex[:12]
            name = input("Give this conversation a name: ").strip() or f"Thread-{thread_id[:6]}"
            _save_thread_meta(thread_id, name)
            print(f"\nStarted new thread: {name}\n")
            return {"configurable": {"thread_id": thread_id}}
        elif choice.isdigit() and 1 <= int(choice) <= len(threads):
            tid, name, _, _, *_pick_rest2 = threads[int(choice) - 1]
            _save_thread_meta(tid, name)  # bump updated_at
            print(f"\nResuming thread: {name}\n")
            return {"configurable": {"thread_id": tid}}
        else:
            print("Invalid choice, try again.")
