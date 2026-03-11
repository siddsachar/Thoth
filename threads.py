from langgraph.checkpoint.sqlite import SqliteSaver
import logging
import sqlite3
import uuid
import os
import pathlib
from datetime import datetime

logger = logging.getLogger(__name__)

# Store data in %APPDATA%/Thoth (writable even when app is in Program Files)
DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(DATA_DIR / "threads.db")

def _init_thread_db():
    """Create a metadata table to store thread names/timestamps."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS thread_meta "
            "(thread_id TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.commit()
        conn.close()
        logger.debug("Thread database initialised at %s", DB_PATH)
    except Exception:
        logger.error("Failed to initialise thread database at %s", DB_PATH, exc_info=True)

def _list_threads():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT thread_id, name, created_at, updated_at FROM thread_meta ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return rows

def _save_thread_meta(thread_id: str, name: str):
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

_init_thread_db()

def _delete_thread(thread_id: str):
    """Remove a thread's metadata from the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM thread_meta WHERE thread_id = ?", (thread_id,))
    conn.commit()
    conn.close()


conn = sqlite3.connect(DB_PATH, check_same_thread=False)
checkpointer = SqliteSaver(conn)


def pick_or_create_thread() -> dict:
    """Interactive menu to resume an existing thread or start a new one."""
    threads = _list_threads()
    print("\n=== Thoth — Thread Manager ===")
    print("  [0] Start a new conversation")
    for idx, (tid, name, created, updated) in enumerate(threads, start=1):
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
            tid, name, _, _ = threads[int(choice) - 1]
            _save_thread_meta(tid, name)  # bump updated_at
            print(f"\nResuming thread: {name}\n")
            return {"configurable": {"thread_id": tid}}
        else:
            print("Invalid choice, try again.")