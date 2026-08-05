"""Tasks — scheduled and on-demand agent actions with optional channel delivery.

A *task* is a named action (one or more prompts) that can run manually,
on a recurring schedule, or once at a specific time.  Tasks subsume both
the old "workflow" concept (multi-step prompt chains) and the old "timer"
concept (notify-only one-shot reminders).

Key features beyond v2.2.0 workflows
-------------------------------------
* Cron expressions via APScheduler ``CronTrigger``
* One-shot ``at`` field (ISO datetime) for "remind me at 3 PM" style tasks
* ``notify_only`` flag — fire a desktop / channel notification without
  invoking the agent (replaces timer_tool)
* ``delivery_channel`` / ``delivery_target`` — send results to Telegram
  or Email in addition to the always-on desktop + in-app notification
* ``model_override`` — per-task model selection
* ``persistent_thread_id`` — opt-in to reuse the same conversation thread
  across runs

Storage: SQLite at ``~/.row-bot/tasks.db``.
Migration: on first import, existing ``workflows.db`` data is migrated
automatically and the old file is kept as a backup.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import sqlite3
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence, TypeVar

from row_bot.approval_policy import (
    DEFAULT_APPROVAL_MODE,
    legacy_safety_mode_to_approval_mode,
    normalize_approval_mode,
)
from row_bot.data_paths import get_tasks_db_path, get_row_bot_data_dir

if TYPE_CHECKING:
    from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# ── Subtask Depth Tracking ───────────────────────────────────────────────────
_subtask_depth_var: ContextVar[int] = ContextVar("subtask_depth", default=0)
_MAX_SUBTASK_DEPTH = 2

# ── Persistence ──────────────────────────────────────────────────────────────
_DATA_DIR = get_row_bot_data_dir()
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = str(get_tasks_db_path())
_OLD_WF_DB = str(_DATA_DIR / "workflows.db")
_TASK_CONFIG_PATH = str(_DATA_DIR / "task_config.json")
_SCHEMA_VERSION = 1
DEFAULT_WORKFLOW_AGENT_PROFILE_ID = "builtin:row_bot_default"
WORKFLOW_PROFILE_MIGRATION_VERSION = "workflow_profile_v1"
_WORKFLOW_READ_ONLY_DEFAULT_DENY_TOOLS = {
    "calendar",
    "custom_tool_builder",
    "designer",
    "gmail",
    "goal",
    "image_gen",
    "row_bot_updater",
    "task",
    "tracker",
    "video_gen",
    "x",
}
_SCHEMA_LOCK = threading.RLock()
_APPROVAL_CHANNEL_PUSH_LOCK = threading.RLock()
_SCHEMA_READY_PATH: str | None = None
_LAST_SCHEMA_REPAIR: dict[str, object] = {}
F = TypeVar("F", bound=Callable[..., object])

_CREATE_TABLE_SQL = {
    "tasks": """
        CREATE TABLE IF NOT EXISTS tasks (
            id                  TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            description         TEXT DEFAULT '',
            icon                TEXT DEFAULT 'âš¡',
            prompts             TEXT NOT NULL,
            schedule            TEXT,
            at                  TEXT,
            notify_only         INTEGER DEFAULT 0,
            notify_label        TEXT DEFAULT '',
            enabled             INTEGER DEFAULT 1,
            last_run            TEXT,
            created_at          TEXT NOT NULL,
            sort_order          INTEGER DEFAULT 0,
            delivery_channel    TEXT,
            delivery_target     TEXT,
            model_override      TEXT,
            persistent_thread_id TEXT,
            delete_after_run    INTEGER DEFAULT 0,
            allowed_commands    TEXT DEFAULT '[]',
            allowed_recipients  TEXT DEFAULT '[]',
            skills_override     TEXT,
            steps               TEXT DEFAULT '[]',
            safety_mode         TEXT,
            concurrency_group   TEXT,
            trigger             TEXT,
            tools_override      TEXT,
            channels            TEXT,
            advanced_mode       INTEGER DEFAULT 0,
            agent_profile_id    TEXT DEFAULT '',
            profile_migration_status TEXT DEFAULT '',
            profile_migration_note TEXT DEFAULT '',
            profile_migration_snapshot_json TEXT DEFAULT '{}'
        )
    """,
    "task_runs": """
        CREATE TABLE IF NOT EXISTS task_runs (
            id              TEXT PRIMARY KEY,
            task_id         TEXT NOT NULL,
            thread_id       TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            status          TEXT DEFAULT 'running',
            status_message  TEXT DEFAULT '',
            steps_total     INTEGER DEFAULT 0,
            steps_done      INTEGER DEFAULT 0,
            pipeline_state_id TEXT,
            task_name       TEXT DEFAULT '',
            task_icon       TEXT DEFAULT '',
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """,
    "pipeline_state": """
        CREATE TABLE IF NOT EXISTS pipeline_state (
            run_id              TEXT PRIMARY KEY,
            task_id             TEXT NOT NULL,
            thread_id           TEXT NOT NULL,
            current_step_index  INTEGER DEFAULT 0,
            step_outputs        TEXT DEFAULT '{}',
            status              TEXT DEFAULT 'running',
            resume_token        TEXT,
            paused_at           TEXT,
            config              TEXT DEFAULT '{}',
            graph_interrupted   TEXT,
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        )
    """,
    "workflow_drafts": """
        CREATE TABLE IF NOT EXISTS workflow_drafts (
            id          TEXT PRIMARY KEY,
            task_id     TEXT,
            mode        TEXT NOT NULL,
            payload     TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """,
    "approval_requests": """
        CREATE TABLE IF NOT EXISTS approval_requests (
            id              TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL,
            task_id         TEXT NOT NULL,
            step_id         TEXT NOT NULL,
            resume_token    TEXT UNIQUE NOT NULL,
            message         TEXT,
            channel         TEXT,
            status          TEXT DEFAULT 'pending',
            requested_at    TEXT DEFAULT (datetime('now')),
            responded_at    TEXT,
            timeout_at      TEXT,
            response_note   TEXT,
            agent_run_id    TEXT DEFAULT '',
            resume_kind     TEXT DEFAULT '',
            source_label    TEXT DEFAULT '',
            source_thread_id TEXT DEFAULT '',
            parent_thread_id TEXT DEFAULT '',
            approval_payload_json TEXT DEFAULT '{}'
        )
    """,
    "approval_channel_refs": """
        CREATE TABLE IF NOT EXISTS approval_channel_refs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id     TEXT NOT NULL,
            channel         TEXT NOT NULL,
            message_ref     TEXT NOT NULL,
            FOREIGN KEY (approval_id) REFERENCES approval_requests(id) ON DELETE CASCADE
        )
    """,
    "channel_thread_refs": """
        CREATE TABLE IF NOT EXISTS channel_thread_refs (
            thread_id        TEXT PRIMARY KEY,
            channel          TEXT NOT NULL,
            target           TEXT NOT NULL,
            external_conversation_id TEXT DEFAULT '',
            updated_at       TEXT NOT NULL
        )
    """,
    "channel_thread_notifications": """
        CREATE TABLE IF NOT EXISTS channel_thread_notifications (
            key              TEXT PRIMARY KEY,
            thread_id        TEXT NOT NULL,
            channel          TEXT NOT NULL,
            target           TEXT NOT NULL,
            kind             TEXT NOT NULL,
            text             TEXT NOT NULL,
            payload_json     TEXT DEFAULT '{}',
            status           TEXT NOT NULL DEFAULT 'pending',
            attempts         INTEGER DEFAULT 0,
            last_error       TEXT DEFAULT '',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            delivered_at     TEXT DEFAULT ''
        )
    """,
}

_COLUMN_MIGRATIONS = {
    "tasks": [
        ("description", "TEXT DEFAULT ''"),
        ("icon", "TEXT DEFAULT 'âš¡'"),
        ("prompts", "TEXT DEFAULT '[]'"),
        ("schedule", "TEXT"),
        ("at", "TEXT"),
        ("notify_only", "INTEGER DEFAULT 0"),
        ("notify_label", "TEXT DEFAULT ''"),
        ("enabled", "INTEGER DEFAULT 1"),
        ("last_run", "TEXT"),
        ("created_at", "TEXT DEFAULT ''"),
        ("sort_order", "INTEGER DEFAULT 0"),
        ("delivery_channel", "TEXT"),
        ("delivery_target", "TEXT"),
        ("persistent_thread_id", "TEXT"),
        ("delete_after_run", "INTEGER DEFAULT 0"),
        ("allowed_commands", "TEXT DEFAULT '[]'"),
        ("allowed_recipients", "TEXT DEFAULT '[]'"),
        ("model_override", "TEXT"),
        ("skills_override", "TEXT"),
        ("steps", "TEXT DEFAULT '[]'"),
        ("safety_mode", "TEXT"),
        ("concurrency_group", "TEXT"),
        ("trigger", "TEXT"),
        ("tools_override", "TEXT"),
        ("channels", "TEXT"),
        ("advanced_mode", "INTEGER DEFAULT 0"),
        ("agent_profile_id", "TEXT DEFAULT ''"),
        ("profile_migration_status", "TEXT DEFAULT ''"),
        ("profile_migration_note", "TEXT DEFAULT ''"),
        ("profile_migration_snapshot_json", "TEXT DEFAULT '{}'"),
    ],
    "task_runs": [
        ("finished_at", "TEXT"),
        ("status", "TEXT DEFAULT 'running'"),
        ("status_message", "TEXT DEFAULT ''"),
        ("steps_total", "INTEGER DEFAULT 0"),
        ("steps_done", "INTEGER DEFAULT 0"),
        ("task_name", "TEXT DEFAULT ''"),
        ("task_icon", "TEXT DEFAULT ''"),
        ("pipeline_state_id", "TEXT"),
    ],
    "pipeline_state": [
        ("current_step_index", "INTEGER DEFAULT 0"),
        ("step_outputs", "TEXT DEFAULT '{}'"),
        ("status", "TEXT DEFAULT 'running'"),
        ("resume_token", "TEXT"),
        ("paused_at", "TEXT"),
        ("config", "TEXT DEFAULT '{}'"),
        ("graph_interrupted", "TEXT"),
        ("created_at", "TEXT DEFAULT ''"),
        ("updated_at", "TEXT DEFAULT ''"),
    ],
    "approval_requests": [
        ("responded_at", "TEXT"),
        ("timeout_at", "TEXT"),
        ("response_note", "TEXT"),
        ("agent_run_id", "TEXT DEFAULT ''"),
        ("resume_kind", "TEXT DEFAULT ''"),
        ("source_label", "TEXT DEFAULT ''"),
        ("source_thread_id", "TEXT DEFAULT ''"),
        ("parent_thread_id", "TEXT DEFAULT ''"),
        ("approval_payload_json", "TEXT DEFAULT '{}'"),
    ],
}

_REQUIRED_COLUMNS = {
    "tasks": {
        "id", "name", "description", "icon", "prompts", "schedule", "at",
        "notify_only", "notify_label", "enabled", "last_run", "created_at",
        "sort_order", "delivery_channel", "delivery_target", "model_override",
        "persistent_thread_id", "delete_after_run", "allowed_commands",
        "allowed_recipients", "skills_override", "steps", "safety_mode",
        "concurrency_group", "trigger", "tools_override", "channels",
        "advanced_mode", "agent_profile_id", "profile_migration_status",
        "profile_migration_note", "profile_migration_snapshot_json",
    },
    "task_runs": {
        "id", "task_id", "thread_id", "started_at", "finished_at", "status",
        "status_message", "steps_total", "steps_done", "pipeline_state_id",
        "task_name", "task_icon",
    },
    "pipeline_state": {
        "run_id", "task_id", "thread_id", "current_step_index",
        "step_outputs", "status", "resume_token", "paused_at", "config",
        "graph_interrupted", "created_at", "updated_at",
    },
    "workflow_drafts": {"id", "task_id", "mode", "payload", "updated_at"},
    "approval_requests": {
        "id", "run_id", "task_id", "step_id", "resume_token", "message",
        "channel", "status", "requested_at", "responded_at", "timeout_at",
        "response_note", "agent_run_id", "resume_kind", "source_label",
        "source_thread_id", "parent_thread_id", "approval_payload_json",
    },
    "approval_channel_refs": {"id", "approval_id", "channel", "message_ref"},
    "channel_thread_refs": {
        "thread_id", "channel", "target", "external_conversation_id", "updated_at",
    },
    "channel_thread_notifications": {
        "key", "thread_id", "channel", "target", "kind", "text", "payload_json",
        "status", "attempts", "last_error", "created_at", "updated_at", "delivered_at",
    },
}


def _raw_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except Exception:
        conn.close()
        raise


def _is_corrupt_db_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, sqlite3.DatabaseError)
        and (
            "file is not a database" in text
            or "database disk image is malformed" in text
            or "database is malformed" in text
        )
    )


def _is_schema_operational_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "no such table" in text
        or "no such column" in text
        or "database schema has changed" in text
        or "schema" in text
    )


def _has_invalid_sqlite_header(path: pathlib.Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        return path.read_bytes()[:16] != b"SQLite format 3\x00"
    except OSError:
        return False


def _backup_task_db_files(reason: str) -> pathlib.Path:
    data_dir = pathlib.Path(_DB_PATH).expanduser().parent
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / "recovery" / f"tasks-db-{reason}-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = pathlib.Path(_DB_PATH + suffix)
        if src.exists():
            shutil.move(str(src), str(backup_dir / src.name))
    return backup_dir


def backup_and_recreate_task_db(reason: str = "reset") -> pathlib.Path | None:
    """Back up task DB files, recreate a clean schema, and return the backup."""
    global _SCHEMA_READY_PATH
    with _SCHEMA_LOCK:
        backup_dir = _backup_task_db_files(reason)
        _SCHEMA_READY_PATH = None
        ensure_task_schema(repair=True, force=True)
        logger.warning(
            "Task DB recreated after %s; previous files moved to %s",
            reason,
            backup_dir,
        )
        return backup_dir


def _apply_schema(conn: sqlite3.Connection) -> None:
    for sql in _CREATE_TABLE_SQL.values():
        conn.execute(sql)
    for table, migrations in _COLUMN_MIGRATIONS.items():
        for col, defn in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
                logger.info("Migrated %s table: added '%s' column", table, col)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    conn.execute(
        "UPDATE tasks SET safety_mode = 'allow_all' WHERE safety_mode IS NULL"
    )
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def _schema_snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    columns_by_table: dict[str, list[str]] = {}
    for table, required in _REQUIRED_COLUMNS.items():
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        columns_by_table[table] = cols
        missing = sorted(required - set(cols))
        if missing:
            missing_columns[table] = missing
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {
        "tables": sorted(tables),
        "missing_tables": missing_tables,
        "columns_by_table": columns_by_table,
        "missing_columns": missing_columns,
        "user_version": user_version,
    }


def _validate_schema(conn: sqlite3.Connection) -> dict[str, object]:
    snapshot = _schema_snapshot(conn)
    if snapshot["missing_tables"] or snapshot["missing_columns"]:
        raise RuntimeError(
            "task schema incomplete: "
            f"missing_tables={snapshot['missing_tables']} "
            f"missing_columns={snapshot['missing_columns']}"
        )
    return snapshot


def ensure_task_schema(*, repair: bool = True, force: bool = False) -> dict[str, object]:
    """Ensure all required task DB tables and columns exist."""
    global _SCHEMA_READY_PATH, _LAST_SCHEMA_REPAIR
    db_path = str(pathlib.Path(_DB_PATH).expanduser())
    with _SCHEMA_LOCK:
        if not force and _SCHEMA_READY_PATH == db_path:
            return {"status": "ok", "db_path": db_path, "cached": True}

        db_file = pathlib.Path(db_path)
        existed_before = db_file.exists()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        recreated = False
        try:
            if _has_invalid_sqlite_header(db_file):
                raise sqlite3.DatabaseError("file is not a database")
            conn = _raw_conn()
            try:
                before = _schema_snapshot(conn)
                _apply_schema(conn)
                after = _validate_schema(conn)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            if repair and _is_corrupt_db_error(exc):
                backup_dir = _backup_task_db_files("corrupt")
                _LAST_SCHEMA_REPAIR = {
                    "status": "recreated",
                    "reason": str(exc),
                    "backup_dir": str(backup_dir),
                    "at": datetime.now().isoformat(),
                }
                logger.warning(
                    "Task DB was corrupt; moved files to %s and recreating",
                    backup_dir,
                )
                recreated = True
                conn = _raw_conn()
                try:
                    before = _schema_snapshot(conn)
                    _apply_schema(conn)
                    after = _validate_schema(conn)
                    conn.commit()
                finally:
                    conn.close()
            else:
                _SCHEMA_READY_PATH = None
                logger.exception("Task DB schema check failed for %s", db_path)
                raise

        _migrate_from_workflows()
        _SCHEMA_READY_PATH = db_path
        repaired = bool(
            not recreated
            and existed_before
            and (before["missing_tables"] or before["missing_columns"])
        )
        initialized = bool(
            not recreated
            and not existed_before
            and (before["missing_tables"] or before["missing_columns"])
        )
        if repaired:
            _LAST_SCHEMA_REPAIR = {
                "status": "repaired",
                "missing_tables": before["missing_tables"],
                "missing_columns": before["missing_columns"],
                "at": datetime.now().isoformat(),
            }
            logger.warning(
                "Task DB schema repaired in place at %s: tables=%s columns=%s",
                db_path,
                before["missing_tables"],
                before["missing_columns"],
            )
        elif recreated:
            logger.warning("Task DB schema recreated at %s", db_path)
        elif initialized:
            logger.info("Task DB schema initialized at %s", db_path)
        else:
            logger.info("Task DB schema ok at %s", db_path)
        return {
            "status": (
                "recreated" if recreated else
                "repaired" if repaired else
                "initialized" if initialized else
                "ok"
            ),
            "db_path": db_path,
            "before": before,
            "after": after,
            "last_repair": dict(_LAST_SCHEMA_REPAIR),
        }


def diagnose_task_schema() -> dict[str, object]:
    """Return support diagnostics for tasks.db."""
    path = pathlib.Path(_DB_PATH).expanduser()
    info: dict[str, object] = {
        "db_path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "wal_exists": pathlib.Path(str(path) + "-wal").exists(),
        "shm_exists": pathlib.Path(str(path) + "-shm").exists(),
        "last_repair": dict(_LAST_SCHEMA_REPAIR),
    }
    try:
        conn = _raw_conn()
        try:
            snapshot = _schema_snapshot(conn)
        finally:
            conn.close()
        info.update(snapshot)
        info["ok"] = not snapshot["missing_tables"] and not snapshot["missing_columns"]
    except Exception as exc:
        info["ok"] = False
        info["error"] = str(exc)
    return info


def _schema_retry(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ensure_task_schema()
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if not _is_schema_operational_error(exc):
                raise
            logger.warning(
                "Task DB schema error in %s; repairing and retrying once: %s",
                fn.__name__,
                exc,
            )
            ensure_task_schema(repair=True, force=True)
            return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _get_conn() -> sqlite3.Connection:
    ensure_task_schema()
    return _raw_conn()


def _init_db() -> None:
    """Create the tasks and task_runs tables if they don't exist."""
    ensure_task_schema(repair=True, force=True)
    return
    conn = _raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id                  TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            description         TEXT DEFAULT '',
            icon                TEXT DEFAULT '⚡',
            prompts             TEXT NOT NULL,          -- JSON list of prompt strings
            schedule            TEXT,                   -- recurring: daily:HH:MM / weekly:DAY:HH:MM / interval:H / cron:EXPR
            at                  TEXT,                   -- one-shot ISO datetime (mutually exclusive with schedule)
            notify_only         INTEGER DEFAULT 0,      -- 1 = fire notification only, no agent invocation
            notify_label        TEXT DEFAULT '',         -- label for notify-only tasks (replaces timer label)
            enabled             INTEGER DEFAULT 1,
            last_run            TEXT,
            created_at          TEXT NOT NULL,
            sort_order          INTEGER DEFAULT 0,
            delivery_channel    TEXT,                   -- null / 'telegram' / 'email'
            delivery_target     TEXT,                   -- chat_id or email address
            model_override      TEXT,                   -- null = use global default model
            persistent_thread_id TEXT,                  -- null = fresh thread each run
            delete_after_run    INTEGER DEFAULT 0,      -- 1 = auto-delete after one-shot execution
            allowed_commands    TEXT DEFAULT '[]',      -- JSON list of allowed shell command prefixes for background runs
            allowed_recipients  TEXT DEFAULT '[]',      -- JSON list of allowed email recipients for background runs
            skills_override     TEXT,                   -- JSON list of skill names (null = use global)
            steps               TEXT DEFAULT '[]',      -- JSON list of step dicts (pipeline mode)
            safety_mode         TEXT,                   -- null = inherit global / 'block' / 'approve' / 'allow_all'
            concurrency_group   TEXT,                   -- null = no limit / 'local_gpu' / custom
            trigger             TEXT,                   -- JSON trigger config (null = schedule/manual only)
            tools_override      TEXT,                    -- JSON list of tool names (null = all enabled)
            channels            TEXT,                    -- JSON list of channel names (null = workflow default)
            advanced_mode       INTEGER DEFAULT 0,       -- 1 = reopen in Advanced editor mode
            agent_profile_id    TEXT DEFAULT '',         -- optional Agent Profile id/slug
            profile_migration_status TEXT DEFAULT '',
            profile_migration_note TEXT DEFAULT '',
            profile_migration_snapshot_json TEXT DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_runs (
            id              TEXT PRIMARY KEY,
            task_id         TEXT NOT NULL,
            thread_id       TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            status          TEXT DEFAULT 'running',     -- running / completed / failed / completed_delivery_failed / paused / stopped / cancelled
            status_message  TEXT DEFAULT '',             -- human-readable detail (delivery result, error reason)
            steps_total     INTEGER DEFAULT 0,
            steps_done      INTEGER DEFAULT 0,
            pipeline_state_id TEXT,                     -- FK to pipeline_state for resume support
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_state (
            run_id              TEXT PRIMARY KEY,
            task_id             TEXT NOT NULL,
            thread_id           TEXT NOT NULL,
            current_step_index  INTEGER DEFAULT 0,
            step_outputs        TEXT DEFAULT '{}',      -- JSON: {step_id: output_text}
            status              TEXT DEFAULT 'running', -- running / paused / completed / failed / stopped
            resume_token        TEXT,                   -- UUID for approval resumption
            paused_at           TEXT,
            config              TEXT DEFAULT '{}',      -- JSON: serialized run config
            graph_interrupted   TEXT,                   -- 'true' if paused by LangGraph interrupt()
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_drafts (
            id          TEXT PRIMARY KEY,
            task_id     TEXT,
            mode        TEXT NOT NULL,
            payload     TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_requests (
            id              TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL,
            task_id         TEXT NOT NULL,
            step_id         TEXT NOT NULL,
            resume_token    TEXT UNIQUE NOT NULL,
            message         TEXT,
            channel         TEXT,
            status          TEXT DEFAULT 'pending',     -- pending / approved / denied / timed_out
            requested_at    TEXT DEFAULT (datetime('now')),
            responded_at    TEXT,
            timeout_at      TEXT,
            response_note   TEXT,
            agent_run_id    TEXT DEFAULT '',
            resume_kind     TEXT DEFAULT '',
            source_label    TEXT DEFAULT '',
            source_thread_id TEXT DEFAULT '',
            parent_thread_id TEXT DEFAULT '',
            approval_payload_json TEXT DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_channel_refs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id     TEXT NOT NULL,
            channel         TEXT NOT NULL,
            message_ref     TEXT NOT NULL,
            FOREIGN KEY (approval_id) REFERENCES approval_requests(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_thread_refs (
            thread_id        TEXT PRIMARY KEY,
            channel          TEXT NOT NULL,
            target           TEXT NOT NULL,
            external_conversation_id TEXT DEFAULT '',
            updated_at       TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_thread_notifications (
            key              TEXT PRIMARY KEY,
            thread_id        TEXT NOT NULL,
            channel          TEXT NOT NULL,
            target           TEXT NOT NULL,
            kind             TEXT NOT NULL,
            text             TEXT NOT NULL,
            payload_json     TEXT DEFAULT '{}',
            status           TEXT NOT NULL DEFAULT 'pending',
            attempts         INTEGER DEFAULT 0,
            last_error       TEXT DEFAULT '',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            delivered_at     TEXT DEFAULT ''
        )
    """)
    # Migrations for pre-existing databases
    # Migrations for tasks table
    for col, defn in [
        ("allowed_commands", "TEXT DEFAULT '[]'"),
        ("allowed_recipients", "TEXT DEFAULT '[]'"),
        ("model_override", "TEXT"),
        ("skills_override", "TEXT"),
        ("steps", "TEXT DEFAULT '[]'"),
        ("safety_mode", "TEXT"),
        ("concurrency_group", "TEXT"),
        ("trigger", "TEXT"),
        ("tools_override", "TEXT"),
        ("channels", "TEXT"),
        ("advanced_mode", "INTEGER DEFAULT 0"),
        ("agent_profile_id", "TEXT DEFAULT ''"),
        ("profile_migration_status", "TEXT DEFAULT ''"),
        ("profile_migration_note", "TEXT DEFAULT ''"),
        ("profile_migration_snapshot_json", "TEXT DEFAULT '{}'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {defn}")
            logger.info("Migrated tasks table: added '%s' column", col)
        except Exception:
            pass  # column already exists

    for col, defn in [
        ("status_message", "TEXT DEFAULT ''"),
        ("task_name", "TEXT DEFAULT ''"),
        ("task_icon", "TEXT DEFAULT ''"),
        ("pipeline_state_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE task_runs ADD COLUMN {col} {defn}")
            logger.info("Migrated task_runs table: added '%s' column", col)
        except Exception:
            pass  # column already exists

    # pipeline_state migrations
    for col, defn in [
        ("graph_interrupted", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE pipeline_state ADD COLUMN {col} {defn}")
            logger.info("Migrated pipeline_state table: added '%s' column", col)
        except Exception:
            pass  # column already exists

    # Existing tasks preserve current behavior — set safety_mode to 'allow_all'
    # where it's currently NULL (new tasks will default to 'block')
    for col, defn in [
        ("agent_run_id", "TEXT DEFAULT ''"),
        ("resume_kind", "TEXT DEFAULT ''"),
        ("source_label", "TEXT DEFAULT ''"),
        ("source_thread_id", "TEXT DEFAULT ''"),
        ("parent_thread_id", "TEXT DEFAULT ''"),
        ("approval_payload_json", "TEXT DEFAULT '{}'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE approval_requests ADD COLUMN {col} {defn}")
            logger.info("Migrated approval_requests table: added '%s' column", col)
        except Exception:
            pass  # column already exists

    try:
        conn.execute(
            "UPDATE tasks SET safety_mode = 'allow_all' "
            "WHERE safety_mode IS NULL"
        )
    except Exception:
        pass

    conn.commit()
    conn.close()


def _migrate_from_workflows() -> None:
    """Migrate data from the old workflows.db to the new tasks.db.

    Runs once — only if workflows.db exists and a marker file has not been
    written yet.  The marker prevents re-migration when the user intentionally
    deletes tasks.db to get fresh defaults.
    """
    _MARKER = os.path.join(_DATA_DIR, ".workflows_migrated")
    if os.path.exists(_MARKER):
        return  # Already migrated in a prior run
    if not os.path.exists(_OLD_WF_DB):
        return

    conn = _raw_conn()
    count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    if count > 0:
        conn.close()
        # DB already has data (e.g. from a previous migration) — mark done
        open(_MARKER, "w").close()
        return

    try:
        old_conn = sqlite3.connect(_OLD_WF_DB, check_same_thread=False)
        old_conn.row_factory = sqlite3.Row

        # Check old schema exists
        tables = {r[0] for r in old_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "workflows" not in tables:
            old_conn.close()
            conn.close()
            return

        rows = old_conn.execute("SELECT * FROM workflows").fetchall()
        migrated = 0
        for row in rows:
            try:
                d = dict(row)
                conn.execute(
                    "INSERT INTO tasks "
                    "(id, name, description, icon, prompts, schedule, enabled, "
                    "last_run, created_at, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        d["id"], d["name"], d.get("description", ""),
                        d.get("icon", "âš¡"), d["prompts"],
                        d.get("schedule"), d.get("enabled", 1),
                        d.get("last_run"), d["created_at"],
                        d.get("sort_order", 0),
                    ),
                )
                migrated += 1
            except Exception as exc:
                logger.warning("Skipping malformed legacy workflow row: %s", exc)
            continue
            d = dict(row)
            conn.execute(
                "INSERT INTO tasks "
                "(id, name, description, icon, prompts, schedule, enabled, "
                "last_run, created_at, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    d["id"], d["name"], d.get("description", ""),
                    d.get("icon", "⚡"), d["prompts"],
                    d.get("schedule"), d.get("enabled", 1),
                    d.get("last_run"), d["created_at"],
                    d.get("sort_order", 0),
                ),
            )

        # Migrate run history
        if "workflow_runs" in tables:
            runs = old_conn.execute("SELECT * FROM workflow_runs").fetchall()
            for run in runs:
                try:
                    r = dict(run)
                    conn.execute(
                        "INSERT INTO task_runs "
                        "(id, task_id, thread_id, started_at, finished_at, "
                        "status, steps_total, steps_done) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            r["id"], r["workflow_id"], r["thread_id"],
                            r["started_at"], r.get("finished_at"),
                            r.get("status", "completed"),
                            r.get("steps_total", 0), r.get("steps_done", 0),
                        ),
                    )
                except Exception as exc:
                    logger.warning("Skipping malformed legacy workflow run row: %s", exc)
                continue
                r = dict(run)
                conn.execute(
                    "INSERT INTO task_runs "
                    "(id, task_id, thread_id, started_at, finished_at, "
                    "status, steps_total, steps_done) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["id"], r["workflow_id"], r["thread_id"],
                        r["started_at"], r.get("finished_at"),
                        r.get("status", "completed"),
                        r.get("steps_total", 0), r.get("steps_done", 0),
                    ),
                )

        conn.commit()
        old_conn.close()
        # Mark migration done so we never re-migrate if the user deletes tasks.db
        open(_MARKER, "w").close()
        logger.info(
            "Migrated %d tasks from workflows.db → tasks.db", len(rows)
        )
    except Exception as exc:
        logger.warning("Workflow migration failed (non-fatal): %s", exc)
    finally:
        conn.close()


def _ordered_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _json_list_value(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            loaded = json.loads(text)
        except Exception:
            return _ordered_text_list(text)
        return _ordered_text_list(loaded)
    return _ordered_text_list(value)


def _default_task_skill_names_for_migration() -> set[str]:
    defaults = {"proactive_agent"}
    try:
        from row_bot.skills import get_default_active_skill_names

        defaults.update(get_default_active_skill_names("task"))
    except Exception:
        logger.debug("Could not load default task skills for workflow migration", exc_info=True)
    return {name for name in defaults if name}


def _custom_legacy_skill_names(skills_override: Sequence[str] | None) -> list[str]:
    names = _ordered_text_list(skills_override)
    if not names:
        return []
    default_names = _default_task_skill_names_for_migration()
    custom = [name for name in names if name not in default_names]
    if custom:
        return custom
    return []


def _workflow_policy_key(
    *,
    tools_override: Sequence[str] | None,
    skills_override: Sequence[str] | None,
) -> dict[str, list[str]]:
    return {
        "tools": sorted(_ordered_text_list(tools_override)),
        "skills": sorted(_custom_legacy_skill_names(skills_override)),
    }


def _infer_migrated_profile_capability(tools: Sequence[str], skills: Sequence[str]) -> str:
    if not tools:
        return "orchestrator" if skills else "read_only"
    write_or_delivery_tools = {
        "calendar",
        "custom_tool_builder",
        "designer",
        "gmail",
        "goal",
        "image_gen",
        "row_bot_updater",
        "task",
        "tracker",
        "video_gen",
        "x",
    }
    return "write_capable" if any(tool in write_or_delivery_tools for tool in tools) else "read_only"


def _workflow_policy_signature(
    *,
    tools_override: Sequence[str] | None,
    skills_override: Sequence[str] | None,
) -> dict[str, Any]:
    key = _workflow_policy_key(
        tools_override=tools_override,
        skills_override=skills_override,
    )
    return {
        **key,
        "capability": _infer_migrated_profile_capability(key["tools"], key["skills"]),
    }


def _profile_policy_key(profile: Mapping[str, Any]) -> dict[str, list[str]]:
    tool_policy = profile.get("tool_policy_json") or {}
    skill_policy = profile.get("skill_policy_json") or {}
    if not isinstance(tool_policy, dict):
        tool_policy = {}
    if not isinstance(skill_policy, dict):
        skill_policy = {}
    return {
        "tools": sorted(_ordered_text_list(tool_policy.get("allow_tools"))),
        "skills": sorted(_ordered_text_list(skill_policy.get("skills_override"))),
    }


def _known_workflow_tool_ids() -> set[str]:
    try:
        from row_bot.agent_tool_catalog import list_agent_tool_catalog

        return {
            str(item.get("id") or "")
            for item in list_agent_tool_catalog(include_unavailable=True)
            if str(item.get("id") or "")
        }
    except Exception:
        logger.debug("Could not load Agent Profile tool catalog for migration", exc_info=True)
        return set()


def _known_workflow_skill_names() -> set[str]:
    try:
        from row_bot.skills import get_all_skills, load_skills, skills_loaded

        if not skills_loaded():
            load_skills()
        return {skill.name for skill in get_all_skills()}
    except Exception:
        logger.debug("Could not load skills for workflow migration", exc_info=True)
        return set()


def _missing_legacy_policy_items(
    *,
    tools_override: Sequence[str] | None,
    skills_override: Sequence[str] | None,
) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    tools = _ordered_text_list(tools_override)
    if tools:
        known_tools = _known_workflow_tool_ids()
        if known_tools:
            missing_tools = sorted(tool for tool in tools if tool not in known_tools)
            if missing_tools:
                missing["tools"] = missing_tools
    skills = _custom_legacy_skill_names(skills_override)
    if skills:
        known_skills = _known_workflow_skill_names()
        if known_skills:
            missing_skills = sorted(skill for skill in skills if skill not in known_skills)
            if missing_skills:
                missing["skills"] = missing_skills
    return missing


def _migration_profile_name(signature: Mapping[str, Any]) -> str:
    tools = [str(item) for item in signature.get("tools") or []]
    skills = [str(item) for item in signature.get("skills") or []]
    if tools:
        labels = [tool.replace("_", " ").replace("-", " ").title() for tool in tools[:3]]
        joined = " + ".join(labels)
        return f"Migrated {joined} Tools" if joined else "Migrated Workflow Profile"
    if skills:
        labels = [skill.replace("_", " ").replace("-", " ").title() for skill in skills[:3]]
        joined = " + ".join(labels)
        return f"Migrated {joined} Skills" if joined else "Migrated Workflow Profile"
    return "Migrated Workflow Profile"


def _find_matching_builtin_profile(policy_key: Mapping[str, list[str]]) -> dict[str, Any] | None:
    try:
        from row_bot.agent_profiles import list_agent_profiles

        for profile in list_agent_profiles(enabled_only=True, include_builtins=True):
            if profile.get("source") == "builtin" and _profile_policy_key(profile) == policy_key:
                return profile
    except Exception:
        logger.debug("Could not match built-in Agent Profile for workflow migration", exc_info=True)
    return None


def _find_migration_profile(signature: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        from row_bot.agent_profiles import list_agent_profiles

        for profile in list_agent_profiles(enabled_only=False, include_builtins=False):
            provenance = profile.get("provenance_json") or {}
            if not isinstance(provenance, dict):
                continue
            if provenance.get("migration") != WORKFLOW_PROFILE_MIGRATION_VERSION:
                continue
            if provenance.get("policy_signature") == dict(signature):
                return profile
    except Exception:
        logger.debug("Could not find reusable migration profile", exc_info=True)
    return None


def _append_migration_profile_task_id(profile: Mapping[str, Any], task_id: str) -> None:
    if not profile or str(profile.get("id") or "").startswith("builtin:"):
        return
    try:
        from row_bot.agent_profiles import save_agent_profile

        provenance = dict(profile.get("provenance_json") or {})
        source_task_ids = _ordered_text_list(provenance.get("source_task_ids"))
        if task_id not in source_task_ids:
            provenance["source_task_ids"] = [*source_task_ids, task_id]
            save_agent_profile({**dict(profile), "provenance_json": provenance})
    except Exception:
        logger.debug("Could not update migration profile provenance", exc_info=True)


def _create_migration_profile(signature: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    from row_bot.agent_profiles import _unique_slug, normalize_profile_slug, save_agent_profile

    name = _migration_profile_name(signature)
    slug = _unique_slug(normalize_profile_slug(name) or "migrated_workflow_profile")
    capability = str(signature.get("capability") or "read_only")
    allow_tools = _ordered_text_list(signature.get("tools"))
    skills = _ordered_text_list(signature.get("skills"))
    return save_agent_profile(
        slug=slug,
        display_name=name,
        description="Created by the workflow Agent Profile migration from legacy workflow policy.",
        when_to_use="Use for workflows migrated from legacy tool or skill overrides with this policy.",
        instructions=(
            "Run the workflow using the migrated legacy workflow capability policy. "
            "Review this profile before broad reuse."
        ),
        handoff_contract="Summarize workflow results, approvals, delivery status, and any follow-up needed.",
        source="workflow_created",
        tool_policy_json={
            "capability": capability,
            "allow_tools": allow_tools,
            "allow_delegation": False,
        },
        skill_policy_json={"skills_override": skills},
        context_policy_json={
            "default_context_mode": "auto",
            "include_parent_summary": True,
            "include_selected_messages": False,
            "include_workspace_context": True,
            "max_context_tokens": 0,
        },
        workspace_policy_json={
            "workspace_mode_default": "read_only" if capability == "read_only" else "auto",
            "write_lock_required": capability in {"write_capable", "orchestrator"},
            "worktree_allowed": False,
            "developer_workspace_required": False,
        },
        approval_policy_json={"mode": "inherit"},
        model_policy_json={"mode": "inherit"},
        ui_json={"icon": "rule", "color": "blue-grey", "group": "Migrated"},
        provenance_json={
            "migration": WORKFLOW_PROFILE_MIGRATION_VERSION,
            "source_task_ids": [task_id],
            "policy_signature": dict(signature),
        },
    )


def _resolve_workflow_profile_for_legacy_policy(
    *,
    task_id: str,
    task_name: str = "",
    agent_profile_id: str | None,
    tools_override: Sequence[str] | None,
    skills_override: Sequence[str] | None,
    preserve_existing_profile: bool = False,
) -> dict[str, Any]:
    tools = _ordered_text_list(tools_override)
    skills = _ordered_text_list(skills_override)
    raw_policy_present = tools_override is not None or skills_override is not None
    snapshot = {
        "migration": WORKFLOW_PROFILE_MIGRATION_VERSION,
        "task_id": task_id,
        "task_name": task_name,
        "old_tools_override": tools if tools_override is not None else None,
        "old_skills_override": skills if skills_override is not None else None,
    }

    profile_ref = str(agent_profile_id or "").strip()
    if preserve_existing_profile and profile_ref:
        try:
            from row_bot.agent_profiles import require_agent_profile

            profile = require_agent_profile(profile_ref, enabled_only=True)
            snapshot["selected_profile_id"] = profile["id"]
            snapshot["selected_profile_slug"] = profile["slug"]
            return {
                "profile_id": profile["id"],
                "status": "not_needed",
                "note": "Workflow already had an Agent Profile; legacy overrides were retired.",
                "snapshot": snapshot,
                "clear_old_overrides": True,
                "disable": False,
            }
        except Exception as exc:
            snapshot["invalid_profile_reference"] = profile_ref
            snapshot["invalid_profile_error"] = str(exc)

    policy_key = _workflow_policy_key(
        tools_override=tools,
        skills_override=skills,
    )
    custom_policy = bool(policy_key["tools"] or policy_key["skills"])
    if not custom_policy:
        note = "Assigned Default Agent Profile."
        status = "not_needed"
        if raw_policy_present and skills:
            note = "Ignored legacy task default skill snapshot and assigned Default Agent Profile."
            status = "needs_review"
        snapshot["policy_signature"] = _workflow_policy_signature(
            tools_override=[],
            skills_override=[],
        )
        return {
            "profile_id": DEFAULT_WORKFLOW_AGENT_PROFILE_ID,
            "status": status,
            "note": note,
            "snapshot": snapshot,
            "clear_old_overrides": True,
            "disable": False,
        }

    missing = _missing_legacy_policy_items(
        tools_override=policy_key["tools"],
        skills_override=policy_key["skills"],
    )
    if missing:
        snapshot["missing_policy_items"] = missing
        return {
            "profile_id": profile_ref or "",
            "status": "blocked",
            "note": "Legacy workflow policy references missing tools or skills.",
            "snapshot": snapshot,
            "clear_old_overrides": False,
            "disable": True,
        }

    builtin = _find_matching_builtin_profile(policy_key)
    if builtin:
        snapshot["policy_signature"] = _workflow_policy_signature(
            tools_override=policy_key["tools"],
            skills_override=policy_key["skills"],
        )
        snapshot["selected_profile_id"] = builtin["id"]
        snapshot["selected_profile_slug"] = builtin["slug"]
        return {
            "profile_id": builtin["id"],
            "status": "exact_profile",
            "note": f"Legacy workflow policy matched built-in Agent Profile: {builtin['display_name']}.",
            "snapshot": snapshot,
            "clear_old_overrides": True,
            "disable": False,
        }

    signature = _workflow_policy_signature(
        tools_override=policy_key["tools"],
        skills_override=policy_key["skills"],
    )
    profile = _find_migration_profile(signature)
    if profile is None:
        profile = _create_migration_profile(signature, task_id)
    else:
        _append_migration_profile_task_id(profile, task_id)
    snapshot["policy_signature"] = signature
    snapshot["selected_profile_id"] = profile["id"]
    snapshot["selected_profile_slug"] = profile["slug"]
    return {
        "profile_id": profile["id"],
        "status": "created_profile",
        "note": f"Legacy workflow policy migrated to Agent Profile: {profile['display_name']}.",
        "snapshot": snapshot,
        "clear_old_overrides": True,
        "disable": False,
    }


def _task_row_legacy_policy(row: sqlite3.Row | Mapping[str, Any]) -> tuple[list[str] | None, list[str] | None]:
    raw = dict(row)
    return (
        _json_list_value(raw.get("tools_override")),
        _json_list_value(raw.get("skills_override")),
    )


def migrate_workflow_profile_policies() -> dict[str, Any]:
    """Migrate legacy workflow tool/skill overrides into Agent Profiles."""
    ensure_task_schema()
    conn = _raw_conn()
    migrated = 0
    blocked = 0
    needs_review = 0
    created_or_reused = 0
    try:
        rows = conn.execute(
            "SELECT id, name, agent_profile_id, tools_override, skills_override, "
            "enabled, profile_migration_status FROM tasks"
        ).fetchall()
        for row in rows:
            raw_tools, raw_skills = _task_row_legacy_policy(row)
            profile_ref = str(row["agent_profile_id"] or "")
            status = str(row["profile_migration_status"] or "")
            if (
                status
                and profile_ref
                and raw_tools is None
                and raw_skills is None
            ):
                continue
            conversion = _resolve_workflow_profile_for_legacy_policy(
                task_id=str(row["id"]),
                task_name=str(row["name"] or ""),
                agent_profile_id=profile_ref,
                tools_override=raw_tools,
                skills_override=raw_skills,
                preserve_existing_profile=True,
            )
            new_enabled = 0 if conversion["disable"] else int(bool(row["enabled"]))
            if conversion["status"] == "blocked":
                blocked += 1
            if conversion["status"] == "needs_review":
                needs_review += 1
            if conversion["status"] == "created_profile":
                created_or_reused += 1
            clear_old = bool(conversion["clear_old_overrides"])
            conn.execute(
                "UPDATE tasks SET agent_profile_id = ?, "
                "tools_override = CASE WHEN ? THEN NULL ELSE tools_override END, "
                "skills_override = CASE WHEN ? THEN NULL ELSE skills_override END, "
                "enabled = ?, profile_migration_status = ?, "
                "profile_migration_note = ?, profile_migration_snapshot_json = ? "
                "WHERE id = ?",
                (
                    str(conversion["profile_id"] or profile_ref or DEFAULT_WORKFLOW_AGENT_PROFILE_ID),
                    1 if clear_old else 0,
                    1 if clear_old else 0,
                    new_enabled,
                    conversion["status"],
                    conversion["note"],
                    json.dumps(conversion["snapshot"], sort_keys=True),
                    row["id"],
                ),
            )
            conn.commit()
            migrated += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "migrated": migrated,
        "blocked": blocked,
        "needs_review": needs_review,
        "created_or_reused_profiles": created_or_reused,
    }


def _migrate_workflow_profile_policies_best_effort() -> None:
    try:
        result = migrate_workflow_profile_policies()
        if result.get("migrated"):
            logger.info("Workflow Agent Profile migration result: %s", result)
    except Exception as exc:
        logger.warning("Workflow Agent Profile migration failed (non-fatal): %s", exc)


_init_db()
_migrate_from_workflows()
_migrate_workflow_profile_policies_best_effort()


# ── Template Variables ───────────────────────────────────────────────────────

def expand_template_vars(
    prompt: str,
    task_id: str | None = None,
    prev_output: str = "",
    step_outputs: dict[str, str] | None = None,
) -> str:
    """Replace ``{{variable}}`` placeholders with current values.

    Supports ``{{prev_output}}`` for the previous step's output and
    ``{{step.<step_id>.output}}`` for any named step's output.
    """
    now = datetime.now()
    replacements = {
        "date": now.strftime("%B %d, %Y"),
        "day": now.strftime("%A"),
        "time": now.strftime("%I:%M %p"),
        "month": now.strftime("%B"),
        "year": str(now.year),
        "prev_output": prev_output,
    }
    if task_id:
        replacements["task_id"] = task_id
    result = prompt
    for key, value in replacements.items():
        result = result.replace("{{" + key + "}}", value)
    # Resolve {{step.<step_id>.output}} references
    if step_outputs:
        import re
        def _resolve_step_ref(m):
            sid = m.group(1)
            return step_outputs.get(sid, "")
        result = re.sub(r"\{\{step\.([^.]+)\.output\}\}", _resolve_step_ref, result)
    return result


# ── CRUD ─────────────────────────────────────────────────────────────────────

def _canonicalize_workflow_model_override(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    from row_bot.providers.selection import resolve_catalog_model_selection

    canonical = resolve_catalog_model_selection(
        raw,
        surface="workflow",
        allow_default=True,
        require_agent_ready=True,
    )
    return canonical.ref or None


def _canonicalize_agent_profile_reference(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    from row_bot.agent_profiles import require_agent_profile

    profile = require_agent_profile(raw, enabled_only=True)
    return str(profile["id"])


def _canonicalize_workflow_steps(steps: list[dict] | None) -> list[dict] | None:
    if not steps:
        return steps
    for step in steps:
        if not isinstance(step, dict):
            continue
        if "model_override" in step:
            canonical = _canonicalize_workflow_model_override(step.get("model_override"))
            if canonical:
                step["model_override"] = canonical
            else:
                step.pop("model_override", None)
        # Workflow prompt runtime policy is selected at the workflow level.
        # Delegate steps are explicit child-Agent calls and may choose a helper.
        if step.get("type") != "delegate_agent":
            step.pop("agent_profile_id", None)
    return steps


def _workflow_model_failure_message(model_ref: str | None, exc: Exception) -> str:
    selected_ref = str(model_ref or "").strip()
    details = {
        "selected_ref": selected_ref,
        "provider_id": "",
        "runtime_model": "",
    }
    if selected_ref:
        try:
            from row_bot.providers.resolution import resolve_provider_config

            resolved = resolve_provider_config(selected_ref, allow_legacy_local=True)
            details["selected_ref"] = resolved.selection_ref
            details["provider_id"] = resolved.provider_id
            details["runtime_model"] = resolved.runtime_model
        except Exception as resolve_exc:
            details["resolve_error"] = str(resolve_exc)
    action = (
        "Check that the selected provider is reachable and that the model is "
        "loaded/available. For LM Studio or llama.cpp, load the model and avoid "
        "unloading it while the workflow is running."
    )
    return (
        "Explicit workflow model override failed; not retrying with the default "
        f"provider. selected_ref={details['selected_ref'] or '(empty)'}; "
        f"provider_id={details['provider_id'] or 'unknown'}; "
        f"runtime_model={details['runtime_model'] or 'unknown'}; "
        f"error={exc}; suggested_action={action}"
    )


def _diagnose_model_override_value(value: str | None, surface: str) -> dict:
    raw = str(value or "").strip()
    diagnostic = {
        "raw_value": raw,
        "surface": surface,
        "canonical_ref": "",
        "status": "empty" if not raw else "canonical" if raw.startswith("model:") else "legacy_bare",
        "message": "",
    }
    if not raw:
        return diagnostic
    try:
        from row_bot.providers.selection import canonicalize_model_selection

        canonical = canonicalize_model_selection(raw, surface, allow_default=True)
        diagnostic["canonical_ref"] = canonical.ref
        if raw.startswith("model:"):
            diagnostic["status"] = "canonical"
        elif canonical.ref:
            diagnostic["status"] = "canonicalizable"
            diagnostic["message"] = "Legacy bare value can be canonicalized explicitly."
    except Exception as exc:
        text = str(exc)
        diagnostic["message"] = text
        diagnostic["status"] = "ambiguous" if "Ambiguous model selection" in text else "unknown"
    return diagnostic


def diagnose_legacy_model_overrides() -> list[dict]:
    """Return read-only diagnostics for legacy bare task/thread model overrides."""
    diagnostics: list[dict] = []
    for task in list_tasks():
        task_id = str(task.get("id") or "")
        task_name = str(task.get("name") or "")
        model_override = str(task.get("model_override") or "")
        if model_override and not model_override.startswith("model:"):
            item = _diagnose_model_override_value(model_override, "workflow")
            item.update({"scope": "task", "task_id": task_id, "task_name": task_name})
            diagnostics.append(item)
        for index, step in enumerate(task.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step_override = str(step.get("model_override") or "")
            if not step_override or step_override.startswith("model:"):
                continue
            item = _diagnose_model_override_value(step_override, "workflow")
            item.update({
                "scope": "task_step",
                "task_id": task_id,
                "task_name": task_name,
                "step_index": index,
                "step_id": str(step.get("id") or ""),
            })
            diagnostics.append(item)
    try:
        from row_bot.threads import _list_threads

        for thread_id, name, _created, _updated, model_override, *_rest in _list_threads():
            raw = str(model_override or "")
            if not raw or raw.startswith("model:"):
                continue
            item = _diagnose_model_override_value(raw, "channels")
            item.update({"scope": "thread", "thread_id": str(thread_id), "thread_name": str(name)})
            diagnostics.append(item)
    except Exception:
        logger.debug("Thread model override diagnostics unavailable", exc_info=True)
    return diagnostics


@_schema_retry
def create_task(
    name: str,
    prompts: list[str] | None = None,
    description: str = "",
    icon: str = "⚡",
    schedule: str | None = None,
    at: str | None = None,
    notify_only: bool = False,
    notify_label: str = "",
    delivery_channel: str | None = None,
    delivery_target: str | None = None,
    model_override: str | None = None,
    persistent_thread_id: str | None = None,
    delete_after_run: bool = False,
    delay_minutes: float | None = None,
    skills_override: list[str] | None = None,
    steps: list[dict] | None = None,
    safety_mode: str = "block",
    concurrency_group: str | None = None,
    trigger: dict | None = None,
    tools_override: list[str] | None = None,
    channels: list[str] | None = None,
    advanced_mode: bool | None = None,
    agent_profile_id: str | None = None,
    enabled: bool = True,
    apply_default_skills: bool = True,
) -> str:
    """Create a new task and return its ID.

    *delay_minutes* is a convenience for quick timers: it computes
    ``at = now + N minutes`` and automatically sets ``delete_after_run``
    so the LLM never needs to compute an ISO datetime.

    Only ONE of *schedule*, *at*, *delay_minutes* may be provided.
    """
    # ── Mutual-exclusivity check ──────────────────────────────────────
    _set_count = sum(1 for v in (schedule, at, delay_minutes) if v)
    if _set_count > 1:
        raise ValueError(
            "Only one of schedule, at, or delay_minutes may be set."
        )

    # ── delay_minutes → at conversion ────────────────────────────────
    if delay_minutes is not None:
        if delay_minutes <= 0:
            raise ValueError("delay_minutes must be positive.")
        at = (datetime.now() + timedelta(minutes=delay_minutes)).isoformat()
        delete_after_run = True  # one-shot timers auto-delete

    # ── Validate delivery settings ────────────────────────────────────
    _validate_delivery(delivery_channel, delivery_target)

    task_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    if prompts is None:
        prompts = []
    if advanced_mode is None:
        advanced_mode = bool(steps)
    model_override = _canonicalize_workflow_model_override(model_override)
    legacy_policy_input = tools_override is not None or skills_override is not None
    agent_profile_id = _canonicalize_agent_profile_reference(
        agent_profile_id or DEFAULT_WORKFLOW_AGENT_PROFILE_ID
    )
    safety_mode = legacy_safety_mode_to_approval_mode(safety_mode)
    profile_migration_status = "not_needed"
    profile_migration_note = "Profile-first workflow."
    profile_migration_snapshot: dict[str, Any] = {}
    if notify_only:
        skills_override = None
        tools_override = None
    elif legacy_policy_input:
        conversion = _resolve_workflow_profile_for_legacy_policy(
            task_id=task_id,
            task_name=name,
            agent_profile_id=agent_profile_id,
            tools_override=tools_override,
            skills_override=skills_override,
            preserve_existing_profile=False,
        )
        agent_profile_id = str(conversion["profile_id"] or agent_profile_id)
        profile_migration_status = str(conversion["status"] or "not_needed")
        profile_migration_note = str(conversion["note"] or "")
        profile_migration_snapshot = dict(conversion["snapshot"] or {})
        if conversion["clear_old_overrides"]:
            tools_override = None
            skills_override = None
        if conversion["disable"]:
            enabled = False
    else:
        skills_override = None
        tools_override = None
    # If steps provided, also sync prompts for backward compat
    if steps:
        assign_step_ids(steps)
        _canonicalize_workflow_steps(steps)
        prompts = _steps_to_prompts(steps) or prompts
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tasks "
        "(id, name, description, icon, prompts, schedule, at, notify_only, "
        "notify_label, delivery_channel, delivery_target, model_override, "
        "persistent_thread_id, delete_after_run, created_at, enabled, skills_override, "
        "steps, safety_mode, concurrency_group, trigger, tools_override, channels, "
        "advanced_mode, agent_profile_id, profile_migration_status, "
        "profile_migration_note, profile_migration_snapshot_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, name, description, icon, json.dumps(prompts),
            schedule, at, int(notify_only), notify_label,
            delivery_channel, delivery_target, model_override,
            persistent_thread_id, int(delete_after_run), now, int(enabled),
            json.dumps(skills_override) if skills_override is not None else None,
            json.dumps(steps) if steps else "[]",
            safety_mode,
            concurrency_group,
            json.dumps(trigger) if trigger else None,
            json.dumps(tools_override) if tools_override else None,
            json.dumps(channels) if channels is not None else None,
            int(bool(advanced_mode)),
            agent_profile_id,
            profile_migration_status,
            profile_migration_note,
            json.dumps(profile_migration_snapshot, sort_keys=True),
        ),
    )
    conn.commit()
    conn.close()

    # Sync APScheduler job (no-op if scheduler not yet started)
    if _scheduler is not None:
        task = get_task(task_id)
        if task:
            _sync_job(task)

    return task_id


@_schema_retry
def get_task(task_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_dict(row)


@_schema_retry
def list_tasks() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT t.*, "
        "(SELECT r.status FROM task_runs r WHERE r.task_id = t.id "
        " ORDER BY r.started_at DESC LIMIT 1) AS last_status "
        "FROM tasks t ORDER BY t.sort_order, t.created_at"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


@_schema_retry
def update_task(task_id: str, **kwargs) -> None:
    """Update task fields.

    Accepted keys: name, description, icon, prompts (list[str]), schedule,
    at, notify_only, notify_label, enabled, sort_order, last_run,
    delivery_channel, delivery_target, model_override,
    persistent_thread_id, delete_after_run.
    """
    _ALLOWED = {
        "name", "description", "icon", "prompts", "schedule", "at",
        "notify_only", "notify_label", "enabled", "sort_order", "last_run",
        "delivery_channel", "delivery_target", "model_override",
        "persistent_thread_id", "delete_after_run",
        "allowed_commands", "allowed_recipients",
        "skills_override",
        "steps", "safety_mode", "concurrency_group", "trigger",
        "tools_override", "channels", "advanced_mode", "agent_profile_id",
        "profile_migration_status", "profile_migration_note",
        "profile_migration_snapshot_json",
    }

    # ── Validate delivery if either field is being changed ───────────
    if {"tools_override", "skills_override"} & set(kwargs):
        current = get_task(task_id) or {}
        conversion = _resolve_workflow_profile_for_legacy_policy(
            task_id=task_id,
            task_name=str(kwargs.get("name") or current.get("name") or ""),
            agent_profile_id=str(kwargs.get("agent_profile_id") or current.get("agent_profile_id") or ""),
            tools_override=kwargs.get("tools_override", current.get("tools_override")),
            skills_override=kwargs.get("skills_override", current.get("skills_override")),
            preserve_existing_profile=False,
        )
        kwargs["agent_profile_id"] = str(
            conversion["profile_id"]
            or current.get("agent_profile_id")
            or DEFAULT_WORKFLOW_AGENT_PROFILE_ID
        )
        kwargs["profile_migration_status"] = str(conversion["status"] or "not_needed")
        kwargs["profile_migration_note"] = str(conversion["note"] or "")
        kwargs["profile_migration_snapshot_json"] = dict(conversion["snapshot"] or {})
        if conversion["clear_old_overrides"]:
            kwargs["tools_override"] = None
            kwargs["skills_override"] = None
        if conversion["disable"]:
            kwargs["enabled"] = False

    if "delivery_channel" in kwargs or "delivery_target" in kwargs:
        # Merge with existing values to get full picture
        task = get_task(task_id)
        if task:
            ch = kwargs.get("delivery_channel", task.get("delivery_channel"))
            tgt = kwargs.get("delivery_target", task.get("delivery_target"))
            _validate_delivery(ch, tgt)

    conn = _get_conn()
    for key, value in kwargs.items():
        if key not in _ALLOWED:
            continue
        if key == "model_override":
            value = _canonicalize_workflow_model_override(value)
        if key == "safety_mode":
            value = legacy_safety_mode_to_approval_mode(value)
        if key == "agent_profile_id":
            value = _canonicalize_agent_profile_reference(value or DEFAULT_WORKFLOW_AGENT_PROFILE_ID)
        if key == "steps" and isinstance(value, list):
            assign_step_ids(value)
            _canonicalize_workflow_steps(value)
        if key in ("prompts", "allowed_commands", "allowed_recipients",
                   "skills_override", "steps", "trigger",
                   "tools_override", "channels", "profile_migration_snapshot_json"):
            value = json.dumps(value, sort_keys=True) if value is not None else None
        if key in ("notify_only", "delete_after_run", "advanced_mode"):
            value = int(value)
        conn.execute(
            f"UPDATE tasks SET {key} = ? WHERE id = ?",
            (value, task_id),
        )
    conn.commit()
    conn.close()

    # Re-sync APScheduler job if schedule-related fields changed
    _SCHEDULE_KEYS = {"schedule", "at", "enabled", "notify_only", "delete_after_run"}
    if _scheduler is not None and _SCHEDULE_KEYS & set(kwargs):
        task = get_task(task_id)
        if task:
            _sync_job(task)


@_schema_retry
def delete_task(task_id: str) -> None:
    _remove_job(task_id)
    conn = _get_conn()
    # Clean up pipeline_state and cancel pending approval_requests up-front
    # so the cleanup is co-located with the DELETE that removes the task row.
    # Task runs are preserved for audit history (get_recent_runs handles
    # orphaned runs via COALESCE to "(deleted)").
    conn.execute("DELETE FROM pipeline_state WHERE task_id = ?", (task_id,))
    conn.execute(
        "UPDATE approval_requests SET status = 'cancelled', responded_at = ? "
        "WHERE task_id = ? AND status = 'pending'",
        (datetime.now().isoformat(), task_id),
    )
    # Collect all threads linked to this task BEFORE we tear down the rows.
    linked_thread_ids: set[str] = set()
    try:
        row = conn.execute(
            "SELECT COALESCE(persistent_thread_id, '') FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row and row[0]:
            linked_thread_ids.add(row[0])
        for (tid,) in conn.execute(
            "SELECT DISTINCT thread_id FROM task_runs "
            "WHERE task_id = ? AND thread_id IS NOT NULL AND thread_id != ''",
            (task_id,),
        ):
            linked_thread_ids.add(tid)
    except Exception:
        logger.debug("Could not enumerate run threads for task %s",
                     task_id, exc_info=True)

    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    # Cascade thread cleanup (LangGraph checkpoints, media, external state).
    if linked_thread_ids:
        try:
            from row_bot.threads import _delete_thread, purge_external_state
            for tid in linked_thread_ids:
                try:
                    purge_external_state(tid)
                    _delete_thread(tid)
                except Exception:
                    logger.exception(
                        "Failed to cascade thread deletion for task %s (thread %s)",
                        task_id, tid,
                    )
        except Exception:
            logger.exception(
                "threads module unavailable while cascading task %s", task_id,
            )


@_schema_retry
def delete_tasks(task_ids: list[str]) -> tuple[int, list[tuple[str, str]]]:
    """Delete several tasks at once.

    Wraps :func:`delete_task` in a loop so the scheduler-job removal,
    pipeline state cleanup, and approval-request cancellation run for
    every id. Returns ``(deleted_count, failures)``.
    """
    deleted = 0
    failures: list[tuple[str, str]] = []
    for tid in task_ids:
        try:
            delete_task(tid)
            deleted += 1
        except Exception as exc:
            failures.append((tid, str(exc)))
    return deleted, failures


def duplicate_task(task_id: str) -> str | None:
    """Clone a task and return the new ID."""
    task = get_task(task_id)
    if not task:
        return None
    return create_task(
        name=f"{task['name']} (copy)",
        prompts=task["prompts"],
        description=task["description"],
        icon=task["icon"],
        schedule=None,  # don't copy schedule
        at=None,
        notify_only=task.get("notify_only", False),
        notify_label=task.get("notify_label", ""),
        delivery_channel=task.get("delivery_channel"),
        delivery_target=task.get("delivery_target"),
        model_override=task.get("model_override"),
        skills_override=task.get("skills_override"),
        steps=task.get("steps"),
        safety_mode=task.get("safety_mode") or "block",
        concurrency_group=task.get("concurrency_group"),
        channels=task.get("channels"),
        advanced_mode=bool(task.get("advanced_mode")),
        agent_profile_id=task.get("agent_profile_id"),
        apply_default_skills=False,
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["prompts"] = json.loads(d["prompts"])
    d["notify_only"] = bool(d.get("notify_only", 0))
    d["delete_after_run"] = bool(d.get("delete_after_run", 0))
    d["enabled"] = bool(d.get("enabled", 1))
    d["advanced_mode"] = bool(d.get("advanced_mode", 0))
    d["allowed_commands"] = json.loads(d.get("allowed_commands") or "[]")
    d["allowed_recipients"] = json.loads(d.get("allowed_recipients") or "[]")
    raw_skills = d.get("skills_override")
    d["skills_override"] = json.loads(raw_skills) if raw_skills else None
    # Pipeline fields
    raw_steps = d.get("steps") or "[]"
    d["steps"] = json.loads(raw_steps)
    # Guard: if steps contains bare strings (malformed), convert them
    if d["steps"] and isinstance(d["steps"][0], str):
        d["steps"] = _prompts_to_steps(d["steps"])
    raw_trigger = d.get("trigger")
    d["trigger"] = json.loads(raw_trigger) if raw_trigger else None
    raw_tools = d.get("tools_override")
    d["tools_override"] = json.loads(raw_tools) if raw_tools else None
    raw_channels = d.get("channels")
    d["channels"] = json.loads(raw_channels) if raw_channels else None
    d["agent_profile_id"] = str(d.get("agent_profile_id") or "")
    d["profile_migration_status"] = str(d.get("profile_migration_status") or "")
    d["profile_migration_note"] = str(d.get("profile_migration_note") or "")
    raw_migration = d.get("profile_migration_snapshot_json")
    try:
        d["profile_migration_snapshot_json"] = json.loads(raw_migration) if raw_migration else {}
    except Exception:
        d["profile_migration_snapshot_json"] = {}
    if d.get("safety_mode"):
        d["safety_mode"] = legacy_safety_mode_to_approval_mode(d.get("safety_mode"))
    # Auto-convert: if steps is empty but prompts exist, synthesize steps
    if not d["steps"] and d["prompts"]:
        d["steps"] = _prompts_to_steps(d["prompts"])
    return d


def _prompts_to_steps(prompts: list[str]) -> list[dict]:
    """Convert a flat list of prompt strings to typed step dicts."""
    steps = []
    for i, prompt in enumerate(prompts):
        steps.append({
            "id": f"step_{i + 1}",
            "type": "prompt",
            "label": "",
            "prompt": prompt,
        })
    return steps


def _steps_to_prompts(steps: list[dict]) -> list[str]:
    """Extract prompt strings from steps (for backward compat).

    Only works when ALL steps are type 'prompt' with linear flow.
    Returns empty list if steps contain non-prompt types.
    """
    prompts = []
    for step in steps:
        if step.get("type") != "prompt":
            return []  # Can't flatten to simple prompts
        prompts.append(step.get("prompt", ""))
    return prompts


# ── Run History ──────────────────────────────────────────────────────────────

def _workflow_agent_profile_ref(task: dict | None) -> str:
    if not task:
        return DEFAULT_WORKFLOW_AGENT_PROFILE_ID
    profile_ref = str(task.get("agent_profile_id") or "")
    if profile_ref:
        return profile_ref
    return DEFAULT_WORKFLOW_AGENT_PROFILE_ID


def _workflow_agent_profile_snapshot(task: dict | None) -> dict[str, Any]:
    profile_ref = _workflow_agent_profile_ref(task)
    try:
        from row_bot.agent_profiles import snapshot_agent_profile

        return snapshot_agent_profile(profile_ref)
    except Exception:
        logger.debug("Could not snapshot workflow Agent Profile %s", profile_ref, exc_info=True)
        return {}


def _workflow_profile_tool_allowlist(profile_snapshot: Mapping[str, Any]) -> list[str]:
    tool_policy = profile_snapshot.get("tool_policy_json") or {}
    if not isinstance(tool_policy, dict):
        return []
    return _ordered_text_list(tool_policy.get("allow_tools"))


def _workflow_default_task_skills() -> list[str]:
    try:
        from row_bot.skills import get_default_active_skill_names

        return _ordered_text_list(get_default_active_skill_names("task"))
    except Exception:
        logger.debug("Could not load default task skills for workflow runtime", exc_info=True)
        return []


def _workflow_profile_skills(profile_snapshot: Mapping[str, Any]) -> list[str]:
    skill_policy = profile_snapshot.get("skill_policy_json") or {}
    if not isinstance(skill_policy, dict):
        return []
    base = _ordered_text_list(skill_policy.get("skills_override"))
    if not base:
        base = _workflow_default_task_skills()
    deny = set(_ordered_text_list(skill_policy.get("deny_skills")))
    return [name for name in base if name not in deny]


def _filter_workflow_tools_for_profile(
    enabled_tool_names: Sequence[str],
    profile_snapshot: Mapping[str, Any],
) -> list[str]:
    requested = _ordered_text_list(enabled_tool_names)
    tool_policy = profile_snapshot.get("tool_policy_json") or {}
    if not isinstance(tool_policy, dict):
        tool_policy = {}
    allow = set(_ordered_text_list(tool_policy.get("allow_tools")))
    capability = str(tool_policy.get("capability") or "read_only")
    filtered = list(requested)
    if capability == "read_only" and not allow:
        filtered = [name for name in filtered if name not in _WORKFLOW_READ_ONLY_DEFAULT_DENY_TOOLS]
    if allow:
        mcp_allowed = "mcp" in allow or any(name.startswith("mcp_") for name in allow)
        filtered = [
            name
            for name in filtered
            if name in allow or (name == "mcp" and mcp_allowed)
        ]
    return filtered


def _workflow_profile_runtime_policy(
    task: dict,
    enabled_tool_names: Sequence[str],
) -> dict[str, Any]:
    from row_bot.agent_profiles import resolve_profile_for_run

    parent_approval = get_task_approval_mode(task)
    resolved = resolve_profile_for_run(
        _workflow_agent_profile_ref(task),
        parent_approval_mode=parent_approval,
        require_enabled=True,
    )
    profile_snapshot = dict(resolved["profile_snapshot"])
    tool_allowlist = _workflow_profile_tool_allowlist(profile_snapshot)
    return {
        "agent_profile_id": str(resolved["profile_id"] or DEFAULT_WORKFLOW_AGENT_PROFILE_ID),
        "agent_profile_slug": str(resolved["profile_slug"] or ""),
        "agent_profile_snapshot": profile_snapshot,
        "approval_mode": normalize_approval_mode(
            str(resolved["effective_approval_mode"] or parent_approval),
            parent_approval,
        ),
        "skills_override": _workflow_profile_skills(profile_snapshot),
        "tool_allowlist": tool_allowlist,
        "effective_tool_names": _filter_workflow_tools_for_profile(
            enabled_tool_names,
            profile_snapshot,
        ),
        "warnings": list(resolved.get("warnings") or []),
    }


def _mirror_workflow_agent_run_start(
    run_id: str,
    *,
    task_id: str,
    thread_id: str,
    steps_total: int,
    task_name: str = "",
) -> None:
    try:
        task = get_task(task_id)
        from row_bot.agent_runs import mirror_workflow_run_start
        profile_snapshot = _workflow_agent_profile_snapshot(task)

        mirror_workflow_run_start(
            run_id,
            task_id=task_id,
            thread_id=thread_id,
            display_name=task_name or (task or {}).get("name", ""),
            steps_total=steps_total,
            profile_id=_workflow_agent_profile_ref(task),
            profile_snapshot_json=profile_snapshot,
            approval_mode=get_task_approval_mode(task) if task else DEFAULT_APPROVAL_MODE,
            model_override=str((task or {}).get("model_override") or ""),
            tools_override=_workflow_profile_tool_allowlist(profile_snapshot) or None,
            skills_override=_workflow_profile_skills(profile_snapshot),
        )
    except Exception:
        logger.debug("Workflow agent-run mirror start failed for %s", run_id, exc_info=True)


def _mirror_workflow_agent_run_progress(
    run_id: str,
    steps_done: int,
    *,
    steps_total: int = 0,
    label: str = "",
) -> None:
    try:
        from row_bot.agent_runs import mirror_workflow_progress

        mirror_workflow_progress(
            run_id,
            steps_done,
            steps_total=steps_total,
            label=label,
        )
    except Exception:
        logger.debug("Workflow agent-run mirror progress failed for %s", run_id, exc_info=True)


def _mirror_workflow_agent_run_finish(
    run_id: str,
    status: str,
    status_message: str = "",
    terminal_reason: str = "",
) -> None:
    try:
        from row_bot.agent_runs import mirror_workflow_finish

        mirror_workflow_finish(run_id, status, status_message, terminal_reason)
    except Exception:
        logger.debug("Workflow agent-run mirror finish failed for %s", run_id, exc_info=True)


@_schema_retry
def _record_run_start(task_id: str, thread_id: str, steps_total: int,
                      task_name: str = "", task_icon: str = "") -> str:
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO task_runs (id, task_id, thread_id, started_at, "
        "status, steps_total, steps_done, task_name, task_icon) "
        "VALUES (?, ?, ?, ?, 'running', ?, 0, ?, ?)",
        (run_id, task_id, thread_id, now, steps_total, task_name, task_icon),
    )
    conn.commit()
    conn.close()
    _mirror_workflow_agent_run_start(
        run_id,
        task_id=task_id,
        thread_id=thread_id,
        steps_total=steps_total,
        task_name=task_name,
    )
    return run_id


@_schema_retry
def _update_run_progress(run_id: str, steps_done: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE task_runs SET steps_done = ? WHERE id = ?",
        (steps_done, run_id),
    )
    conn.commit()
    conn.close()
    _mirror_workflow_agent_run_progress(run_id, steps_done)


_MEMORY_FALLBACK_STATUS_PREFIX = "Memory recall fallback"


def _merge_memory_fallback_status(existing: str, status_message: str) -> str:
    warnings = [
        line
        for line in str(existing or "").splitlines()
        if line.startswith(_MEMORY_FALLBACK_STATUS_PREFIX)
    ]
    parts = [str(status_message or "").strip(), *warnings]
    return "\n".join(part for part in parts if part)


@_schema_retry
def _record_run_recall_notices(run_id: str, generation_id: str) -> None:
    """Persist any degraded memory-recall notice on the workflow run."""
    try:
        from row_bot.memory_policy import consume_recall_fallback_notices

        notices = consume_recall_fallback_notices(generation_id)
    except Exception:
        logger.debug("Could not consume workflow memory fallback notices", exc_info=True)
        return
    if not notices:
        return
    conn = _get_conn()
    row = conn.execute(
        "SELECT status_message FROM task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    existing = str(row[0] or "") if row else ""
    lines = list(existing.splitlines()) if existing else []
    for notice in notices:
        line = (
            f"{_MEMORY_FALLBACK_STATUS_PREFIX} ({notice.get('code') or 'unavailable'}): "
            f"{notice.get('message') or ''} Reason: {notice.get('detail') or ''} "
            f"Next: {notice.get('action') or ''}"
        ).strip()
        if line not in lines:
            lines.append(line)
    conn.execute(
        "UPDATE task_runs SET status_message = ? WHERE id = ?",
        ("\n".join(lines), run_id),
    )
    conn.commit()
    conn.close()


@_schema_retry
def _finish_run(run_id: str, status: str = "completed",
                status_message: str = "", terminal_reason: str = "") -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT status, status_message FROM task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if (
        row
        and str(row["status"] or "")
        in {"completed", "completed_delivery_failed", "failed", "stopped", "blocked"}
        and status not in {"completed", "completed_delivery_failed", "failed", "stopped", "blocked"}
    ):
        conn.close()
        return
    existing_status = str(row["status_message"] or "") if row else ""
    status_message = _merge_memory_fallback_status(existing_status, status_message)
    conn.execute(
        "UPDATE task_runs SET status = ?, status_message = ?, finished_at = ? "
        "WHERE id = ?",
        (status, status_message, datetime.now().isoformat(), run_id),
    )
    # Clean up pipeline_state for terminal statuses (no longer needed)
    if status in ("completed", "completed_delivery_failed", "failed", "stopped", "blocked"):
        conn.execute("DELETE FROM pipeline_state WHERE run_id = ?", (run_id,))
    conn.commit()
    conn.close()
    _mirror_workflow_agent_run_finish(run_id, status, status_message, terminal_reason)


def _emit_buddy_workflow_event(
    status: str,
    *,
    task_id: str = "",
    thread_id: str = "",
    label: str = "",
    error: str = "",
) -> None:
    try:
        from row_bot.buddy.events import BuddyEventType, emit_buddy_event
        event_type = {
            "done": BuddyEventType.WORKFLOW_DONE,
            "error": BuddyEventType.WORKFLOW_ERROR,
            "cancelled": BuddyEventType.WORKFLOW_CANCELLED,
        }.get(status)
        if event_type is None:
            return
        payload = {
            "task_id": task_id,
            "thread_id": thread_id,
            "label": label or (
                "Workflow error" if status == "error"
                else "Workflow cancelled" if status == "cancelled"
                else "Workflow done"
            ),
        }
        if error:
            payload["error"] = error
        emit_buddy_event(event_type, source="tasks", payload=payload)
    except Exception:
        logger.debug("Buddy workflow event failed", exc_info=True)


def _fire_completion_triggers(completed_task_id: str) -> None:
    """Check if any task has a trigger of type 'task_complete' matching this task.
    If so, fire those tasks in the background.
    """
    from row_bot.tools import registry as tool_registry

    all_tasks = list_tasks()
    enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]

    for t in all_tasks:
        trigger = t.get("trigger")
        if not trigger or not isinstance(trigger, dict):
            continue
        if trigger.get("type") != "task_complete":
            continue
        if trigger.get("target_task") != completed_task_id:
            continue
        if not t.get("enabled", True):
            continue

        logger.info(
            "Completion trigger: task '%s' completed → firing '%s'",
            completed_task_id, t["name"],
        )
        thread_id = _prepare_task_thread(t)
        run_task_background(t["id"], thread_id, enabled_tools)


@_schema_retry
def get_run_history(task_id: str, limit: int = 5) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@_schema_retry
def get_recent_runs(limit: int = 10) -> list[dict]:
    """Return the most recent task runs across all tasks.

    Run rows carry their own ``task_name`` / ``task_icon`` so they remain
    visible in the Activity panel even after the parent task is deleted.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT r.id, r.task_id, r.thread_id, r.started_at, r.finished_at, "
        "r.status, r.status_message, r.steps_total, r.steps_done, "
        "COALESCE(NULLIF(r.task_name, ''), t.name, '(deleted)') AS task_name, "
        "COALESCE(NULLIF(r.task_icon, ''), t.icon, '⚡') AS task_icon "
        "FROM task_runs r LEFT JOIN tasks t ON r.task_id = t.id "
        "ORDER BY r.started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@_schema_retry
def get_upcoming_tasks(limit: int = 5) -> list[dict]:
    """Return tasks that have a schedule or an ``at`` time, sorted by next run."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE enabled = 1 "
        "AND (schedule IS NOT NULL OR at IS NOT NULL) "
        "ORDER BY COALESCE(at, '') DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_next_fire_times(limit: int = 10) -> list[dict]:
    """Return upcoming scheduled task fire times from APScheduler."""
    if _scheduler is None:
        return []
    results = []
    for job in _scheduler.get_jobs():
        if not job.id.startswith("task_"):
            continue
        task_id = job.id[5:]  # strip "task_" prefix
        task = get_task(task_id)
        if not task:
            continue
        next_time = job.next_run_time
        if next_time is None:
            continue
        results.append({
            "task_id": task_id,
            "task_name": task["name"],
            "task_icon": task["icon"],
            "next_run": next_time.isoformat(),
            "schedule": task.get("schedule") or task.get("at") or "",
        })
    results.sort(key=lambda x: x["next_run"])
    return results[:limit]


# ── Background Execution Engine ──────────────────────────────────────────────

_active_runs: dict[str, dict] = {}  # thread_id -> {task_id, run_id, step, total, name}
_active_lock = threading.Lock()


def get_running_tasks() -> dict[str, dict]:
    """Return ``{thread_id: {task_id, run_id, step, total, name, icon,
    started_at, step_label, log}}`` for all in-flight task executions."""
    with _active_lock:
        return dict(_active_runs)


def get_task_logs(thread_id: str, last_n: int = 15) -> list[str]:
    """Return the last *last_n* log lines for a running task."""
    with _active_lock:
        info = _active_runs.get(thread_id)
        if info:
            return list(info.get("log", [])[-last_n:])
    return []


def stop_task(thread_id: str) -> bool:
    """Signal a running task to stop.  Returns True if found & signalled."""
    with _active_lock:
        info = _active_runs.get(thread_id)
        if info and "stop_event" in info:
            info["stop_event"].set()
            logger.info("stop_task: signalled stop for thread %s (task %s)",
                        thread_id, info.get("name", "?"))
            _emit_buddy_workflow_event(
                "cancelled",
                task_id=str(info.get("task_id") or ""),
                thread_id=thread_id,
                label=f"Stopping {info.get('name') or 'workflow'}",
            )
            return True
    return False


def get_running_task_thread(task_id: str) -> str | None:
    """Return the thread_id of a currently-running task, or None."""
    with _active_lock:
        for tid, info in _active_runs.items():
            if info.get("task_id") == task_id:
                return tid
    return None


# Backward-compat alias used by app sidebar
get_running_workflows = get_running_tasks


def _validate_delivery(channel: str | None, target: str | None) -> None:
    """Raise ``ValueError`` if delivery settings are invalid.

    Delegates to the channel registry for validation when available.
    """
    if not channel and not target:
        return  # no delivery — valid
    if channel and not target and channel != "telegram":
        raise ValueError(
            f"delivery_channel is '{channel}' but delivery_target is empty."
        )
    if target and not channel:
        raise ValueError(
            "delivery_target is set but delivery_channel is empty."
        )
    # Validate via channel registry
    try:
        from row_bot.channels import registry as _ch_reg
        _ch_reg.validate_delivery(channel, target)
    except ImportError:
        # Fallback: accept known channel names
        if channel not in ("telegram",):
            raise ValueError(
                f"Unknown delivery_channel '{channel}'."
            )


def _load_task_config() -> dict:
    try:
        with open(_TASK_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_task_config(data: dict) -> None:
    with open(_TASK_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_workflow_default_channels() -> list[str]:
    """Return external channels for workflows that inherit defaults.

    The web app is not stored here: it always receives workflow run status.
    Defaults to an empty list, which means "web app only".
    """
    value = _load_task_config().get("workflow_default_channels", [])
    if not isinstance(value, list):
        return []
    return [str(name) for name in value if isinstance(name, str) and name]


def set_workflow_default_channels(channels: list[str] | None) -> None:
    """Save the external channel default for inheriting workflows."""
    data = _load_task_config()
    clean: list[str] = []
    seen: set[str] = set()
    for name in channels or []:
        if not isinstance(name, str) or not name or name in seen:
            continue
        clean.append(name)
        seen.add(name)
    data["workflow_default_channels"] = clean
    _save_task_config(data)


def _workflow_draft_id(task_id: str | None) -> str:
    return task_id or "__new__"


@_schema_retry
def save_workflow_draft(task_id: str | None, payload: dict) -> None:
    """Persist an autosaved workflow editor draft.

    ``task_id is None`` represents the single "new workflow" draft.  Drafts
    are intentionally separate from the canonical tasks table and are cleared
    when the user saves or discards them.
    """
    conn = _get_conn()
    now = datetime.now().isoformat()
    draft_id = _workflow_draft_id(task_id)
    conn.execute(
        "INSERT OR REPLACE INTO workflow_drafts "
        "(id, task_id, mode, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            draft_id,
            task_id,
            "edit" if task_id else "new",
            json.dumps(payload, ensure_ascii=False),
            now,
        ),
    )
    conn.commit()
    conn.close()


@_schema_retry
def get_workflow_draft(task_id: str | None) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM workflow_drafts WHERE id = ?",
        (_workflow_draft_id(task_id),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "mode": row["mode"],
        "payload": payload if isinstance(payload, dict) else {},
        "updated_at": row["updated_at"],
    }


@_schema_retry
def delete_workflow_draft(task_id: str | None) -> None:
    conn = _get_conn()
    conn.execute(
        "DELETE FROM workflow_drafts WHERE id = ?",
        (_workflow_draft_id(task_id),),
    )
    conn.commit()
    conn.close()


def get_effective_task_channel_names(task: dict) -> list[str]:
    """Return configured external channel names for a task.

    ``task["channels"] is None`` means inherit the workflow-level default.
    Legacy ``delivery_channel`` values are treated as explicit overrides so
    older workflows keep their specific destination until they are edited.
    """
    override = task.get("channels")
    if override is None:
        legacy = task.get("delivery_channel")
        if legacy:
            return [legacy]
        return get_workflow_default_channels()
    return [str(name) for name in override if isinstance(name, str) and name]


def get_task_channels(task: dict) -> list:
    """Return running external channel objects for this task.

    ``task["channels"] is None`` inherits the workflow-level default.
    ``task["channels"] == []`` means web app only. The web app always
    receives run status and is not represented by a channel adapter.
    """
    from row_bot.channels import registry as _ch_reg
    # Preserve the old distinction in one place: override is None means
    # inherited defaults, while not override means web app only.
    channel_names = get_effective_task_channel_names(task)
    if not channel_names:
        return []
    selected = set(channel_names)
    return [ch for ch in _ch_reg.running_channels() if ch.name in selected]


def _deliver_to_channel(task: dict, text: str) -> tuple[str, str]:
    """Send task output to the configured delivery channel (if any).

    Uses the channel registry for routing.

    Returns
    -------
    tuple[str, str]
        ``("delivered", detail)`` on success,
        ``("delivery_failed", reason)`` on error,
        or ``("", "")`` if no delivery was configured.
    """
    channel = task.get("delivery_channel")
    target = task.get("delivery_target")
    if not channel:
        return "", ""
    try:
        from row_bot.channels import registry as _ch_reg
        prefix = f"📋 {task['name']}\n\n"
        ch = _ch_reg.get(channel)
        if ch is None:
            raise RuntimeError(f"Unknown channel: {channel}")
        if not ch.is_running():
            raise RuntimeError(f"{ch.display_name} is not running")

        # Resolve target (Telegram uses configured user ID)
        if channel == "telegram":
            from row_bot.channels.telegram import _get_allowed_user_id
            resolved_target = _get_allowed_user_id()
            if resolved_target is None:
                raise RuntimeError("TELEGRAM_USER_ID is not configured")
        else:
            resolved_target = target
            if not resolved_target:
                # No explicit target — fall back to the channel's
                # configured default (we no longer target-filter in
                # the workflow UI).
                try:
                    resolved_target = ch.get_default_target()
                except Exception as exc:
                    raise RuntimeError(
                        f"{ch.display_name} has no default target configured"
                    ) from exc

        ch.send_message(resolved_target, prefix + text)
        logger.info(
            "Delivery to %s succeeded for task %s", channel, task["name"],
        )
        return "delivered", f"Delivered to {channel}"
    except Exception as exc:
        logger.warning(
            "Delivery to %s failed for task %s: %s",
            channel, task["name"], exc,
        )
        return "delivery_failed", f"{channel} delivery failed: {exc}"


def _deliver_to_channels(task: dict, text: str) -> tuple[str, str]:
    """Send task output to all configured channels via ``get_task_channels``.

    Uses the unified ``channels`` field (null = workflow default).
    Falls back to legacy ``delivery_channel`` if ``channels`` is not set
    and ``delivery_channel`` is.

    Returns
    -------
    tuple[str, str]
        ``("delivered", detail)`` on success (at least one channel),
        ``("delivery_failed", reason)`` if all channels failed,
        or ``("", "")`` if no channels are configured/running.
    """
    channels = get_task_channels(task)
    if not channels:
        # Fallback: legacy single-channel field
        if task.get("delivery_channel"):
            return _deliver_to_channel(task, text)
        return "", ""

    prefix = f"📋 {task['name']}\n\n"
    delivered_to: list[str] = []
    failed: list[str] = []

    for ch in channels:
        try:
            # Resolve target per channel type
            if ch.name == "telegram":
                from row_bot.channels.telegram import _get_allowed_user_id
                target = _get_allowed_user_id()
                if target is None:
                    failed.append(f"{ch.display_name} (no user ID)")
                    continue
            else:
                # Prefer task-level delivery_target; fall back to the
                # channel's configured default. No target configured =>
                # delivery fails for that channel.
                target = task.get("delivery_target") or None
                if target is None:
                    try:
                        target = ch.get_default_target()
                    except Exception as exc:
                        logger.debug(
                            "No default target for %s: %s", ch.name, exc,
                        )
                        target = None
                if target is None:
                    failed.append(f"{ch.display_name} (no target configured)")
                    continue

            ch.send_message(target, prefix + text)
            delivered_to.append(ch.display_name)
            logger.info(
                "Delivery to %s succeeded for task '%s'",
                ch.name, task["name"],
            )
        except Exception as exc:
            failed.append(f"{ch.display_name}: {exc}")
            logger.warning(
                "Delivery to %s failed for task '%s': %s",
                ch.name, task["name"], exc,
            )

    if delivered_to:
        detail = "Delivered to " + ", ".join(delivered_to)
        if failed:
            detail += f" (failed: {', '.join(failed)})"
        return "delivered", detail
    elif failed:
        return "delivery_failed", "; ".join(failed)
    return "", ""


def _workflow_final_status_for_delivery(delivery_status: str) -> str:
    return "completed_delivery_failed" if delivery_status == "delivery_failed" else "completed"


def _push_approval_to_channels(task: dict, approval_id: str,
                               resume_token: str,
                               approval_msg: str) -> None:
    """Push a task approval request to all configured channels.

    Uses the unified ``channels`` field (null = workflow default).
    Stores message refs in ``approval_channel_refs`` for cross-channel
    resolution.

    Both button-capable channels (Telegram inline buttons) and text-based
    channels (Slack, Discord, WhatsApp, SMS) receive the request.  Each
    channel's ``send_approval_request`` handles storage of pending state.
    """
    channels = get_task_channels(task)
    for ch in channels:
        try:
            target = ch.get_default_target()
        except Exception:
            logger.debug("Skipping %s — no default target configured", ch.name)
            continue
        try:
            config = {
                "task_name": task["name"],
                "resume_token": resume_token,
                "message": approval_msg,
            }
            msg_ref = ch.send_approval_request(target, {}, config)
            if msg_ref:
                _store_approval_channel_ref(approval_id, ch.name, msg_ref)
        except Exception as exc:
            logger.warning("Failed to push approval to %s: %s", ch.name, exc)


def record_thread_channel_ref(
    thread_id: str,
    *,
    channel: str,
    target: str | int,
    external_conversation_id: str = "",
) -> None:
    """Remember the channel conversation that owns a Row-Bot thread."""

    clean_thread_id = str(thread_id or "").strip()
    clean_channel = str(channel or "").strip()
    clean_target = str(target or "").strip()
    if not clean_thread_id or not clean_channel or not clean_target:
        return
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO channel_thread_refs "
            "(thread_id, channel, target, external_conversation_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "channel = excluded.channel, target = excluded.target, "
            "external_conversation_id = excluded.external_conversation_id, "
            "updated_at = excluded.updated_at",
            (
                clean_thread_id,
                clean_channel,
                clean_target,
                str(external_conversation_id or ""),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_thread_channel_ref(thread_id: str) -> dict | None:
    """Return the channel origin for a Row-Bot thread, if known."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM channel_thread_refs WHERE thread_id = ?",
            (clean_thread_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_channel_thread_notification(key: str) -> dict | None:
    """Return a durable parent-thread channel notification by key."""

    clean_key = str(key or "").strip()
    if not clean_key:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM channel_thread_notifications WHERE key = ?",
            (clean_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_channel_thread_notification(
    *,
    key: str,
    thread_id: str,
    channel: str,
    target: str | int,
    kind: str,
    text: str,
    payload: Mapping[str, Any] | None = None,
) -> dict | None:
    """Create or refresh a durable notification intent for a channel thread."""

    clean_key = str(key or "").strip()
    clean_thread_id = str(thread_id or "").strip()
    clean_channel = str(channel or "").strip()
    clean_target = str(target or "").strip()
    clean_kind = str(kind or "").strip()
    clean_text = str(text or "").strip()
    if not all((clean_key, clean_thread_id, clean_channel, clean_target, clean_kind, clean_text)):
        return None
    now = datetime.now().isoformat()
    payload_text = json.dumps(dict(payload or {}), sort_keys=True)
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channel_thread_notifications "
            "(key, thread_id, channel, target, kind, text, payload_json, status, "
            "attempts, last_error, created_at, updated_at, delivered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', ?, ?, '')",
            (
                clean_key,
                clean_thread_id,
                clean_channel,
                clean_target,
                clean_kind,
                clean_text,
                payload_text,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT status FROM channel_thread_notifications WHERE key = ?",
            (clean_key,),
        ).fetchone()
        status = str(row["status"] if row else "")
        if status != "delivered":
            conn.execute(
                "UPDATE channel_thread_notifications SET "
                "thread_id = ?, channel = ?, target = ?, kind = ?, text = ?, "
                "payload_json = ?, updated_at = ? WHERE key = ?",
                (
                    clean_thread_id,
                    clean_channel,
                    clean_target,
                    clean_kind,
                    clean_text,
                    payload_text,
                    now,
                    clean_key,
                ),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM channel_thread_notifications WHERE key = ?",
            (clean_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_channel_thread_notification_delivered(key: str) -> bool:
    """Mark a parent-thread notification as delivered to its channel."""

    clean_key = str(key or "").strip()
    if not clean_key:
        return False
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE channel_thread_notifications SET status = 'delivered', "
            "last_error = '', updated_at = ?, delivered_at = ? WHERE key = ?",
            (now, now, clean_key),
        )
        changed = conn.total_changes
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def mark_channel_thread_notification_failed(key: str, error: str) -> bool:
    """Record a failed parent-thread channel notification attempt."""

    clean_key = str(key or "").strip()
    if not clean_key:
        return False
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE channel_thread_notifications SET status = 'failed', "
            "attempts = COALESCE(attempts, 0) + 1, last_error = ?, "
            "updated_at = ? WHERE key = ?",
            (str(error or "")[:1000], now, clean_key),
        )
        changed = conn.total_changes
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def list_pending_channel_thread_notifications(limit: int = 50) -> list[dict]:
    """Return pending/failed channel-thread notifications for retry."""

    safe_limit = max(1, min(500, int(limit or 50)))
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM channel_thread_notifications "
            "WHERE status IN ('pending', 'failed') "
            "ORDER BY created_at ASC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _store_approval_channel_ref(approval_id: str, channel: str,
                                message_ref: str) -> None:
    """Store a channel message reference for an approval request."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO approval_channel_refs (approval_id, channel, message_ref) "
        "VALUES (?, ?, ?)",
        (approval_id, channel, message_ref),
    )
    conn.commit()
    conn.close()


def _approval_channel_ref_exists(approval_id: str, channel: str) -> bool:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM approval_channel_refs "
            "WHERE approval_id = ? AND channel = ? LIMIT 1",
            (str(approval_id), str(channel)),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def push_approval_to_parent_channel(approval_id: str) -> bool:
    """Push a child Agent approval to the parent thread's originating channel."""

    rows = get_pending_approvals(approval_id=str(approval_id or ""))
    approval = rows[0] if rows else None
    if not approval:
        return False
    parent_thread_id = str(approval.get("parent_thread_id") or "").strip()
    if not parent_thread_id:
        return False
    ref = get_thread_channel_ref(parent_thread_id)
    if not ref:
        return False
    channel_name = str(ref.get("channel") or "").strip()
    target = str(ref.get("target") or "").strip()
    if not channel_name or not target:
        return False
    with _APPROVAL_CHANNEL_PUSH_LOCK:
        if _approval_channel_ref_exists(str(approval.get("id") or ""), channel_name):
            return True
        try:
            from row_bot.approval_messages import channel_message, payload_from_row
            from row_bot.channels import registry as channel_registry

            ch = channel_registry.get(channel_name)
            if not ch or not ch.is_running():
                return False
            payload = payload_from_row(approval)
            msg_ref = ch.send_approval_request(
                target,
                {},
                {
                    "approval_kind": "agent_run",
                    "task_name": str(
                        payload.get("source_label")
                        or approval.get("source_label")
                        or "Child Agent"
                    ),
                    "message": channel_message(payload),
                    "resume_token": str(approval.get("resume_token") or ""),
                    "approval_id": str(approval.get("id") or ""),
                    "agent_run_id": str(approval.get("agent_run_id") or ""),
                    "parent_thread_id": parent_thread_id,
                },
            )
            if msg_ref:
                _store_approval_channel_ref(
                    str(approval.get("id") or ""),
                    channel_name,
                    str(msg_ref),
                )
            return bool(msg_ref)
        except Exception as exc:
            logger.warning(
                "Failed to push approval %s to parent channel %s: %s",
                approval_id,
                channel_name,
                exc,
            )
            return False


def _resolve_approval_on_channels(approval_id: str, status: str,
                                  source_channel: str = "web") -> None:
    """Update approval messages on all channels except the source.

    Called after ``respond_to_approval()`` to mark the approval as
    resolved on every channel that received the original request.
    """
    conn = _get_conn()
    refs = conn.execute(
        "SELECT channel, message_ref FROM approval_channel_refs "
        "WHERE approval_id = ?",
        (approval_id,),
    ).fetchall()
    conn.close()

    for ref in refs:
        ch_name, msg_ref = ref["channel"], ref["message_ref"]
        if ch_name == source_channel:
            continue  # the source channel already handled its own UI
        try:
            from row_bot.channels import registry as _ch_reg
            ch = _ch_reg.get(ch_name)
            if ch and ch.is_running():
                ch.update_approval_message(msg_ref, status, source=source_channel)
        except Exception as exc:
            logger.warning(
                "Failed to update approval on %s (ref=%s): %s",
                ch_name, msg_ref, exc,
            )


def run_task_background(
    task_id: str,
    thread_id: str,
    enabled_tool_names: list[str],
    start_step: int = 0,
    notification: bool = True,
    resume_step_outputs: dict[str, str] | None = None,
    resume_run_id: str | None = None,
) -> None:
    """Execute a task in a background thread.

    If *resume_run_id* is provided the existing run record is reused
    instead of creating a new one (avoids duplicate Activity entries
    when resuming after approval).

    For multi-step tasks each prompt is sent to the agent via
    ``invoke_agent`` sequentially.  For notify-only tasks, a desktop
    notification is fired immediately with no agent invocation.
    """
    task = get_task(task_id)
    if not task:
        return

    logger.info("run_task_background: starting '%s' (id=%s, step=%d)",
                task.get("name", "?"), task_id[:8], start_step)
    try:
        from row_bot.buddy.events import BuddyEventType, emit_buddy_event
        emit_buddy_event(
            BuddyEventType.WORKFLOW_STARTED,
            source="tasks",
            payload={
                "task_id": task_id,
                "thread_id": thread_id,
                "label": task.get("name", "Workflow running"),
            },
        )
    except Exception:
        logger.debug("Buddy workflow start event failed", exc_info=True)

    # ── Notify-only tasks (timer replacement) ────────────────────────
    if task.get("notify_only"):
        label = task.get("notify_label") or task["name"]
        from row_bot.notifications import notify
        notify(
            title="⏰ Row-Bot Reminder",
            message=label,
            sound="timer",
            icon="⏰",
        )
        # Record run *before* delivery so Activity always has an entry
        run_id = _record_run_start(task_id, thread_id, 0,
                                   task_name=task["name"], task_icon=task["icon"])
        try:
            delivery_status, delivery_detail = _deliver_to_channels(
                task, f"⏰ Reminder: {label}",
            )
        except Exception as exc:
            logger.error("Notify-only delivery crashed for task %s: %s",
                         task["name"], exc)
            delivery_status = "delivery_failed"
            delivery_detail = "channel delivery failed: " + str(exc)
        update_task(task_id, last_run=datetime.now().isoformat())
        final_status = _workflow_final_status_for_delivery(delivery_status)
        _finish_run(run_id, final_status, status_message=delivery_detail)
        _emit_buddy_workflow_event(
            "done",
            task_id=task_id,
            thread_id=thread_id,
            label=task.get("name", "Workflow done"),
        )
        # Fire any tasks triggered by this task's completion
        if final_status.startswith("completed"):
            try:
                _fire_completion_triggers(task_id)
            except Exception as exc_ct:
                logger.error("Completion trigger error for %s: %s",
                             task["name"], exc_ct)
        if delivery_status == "delivery_failed":
            notify(
                title="⚠️ Delivery Failed",
                message=f"{task['name']} — {delivery_detail}",
                sound="timer",
                icon="⚠️",
            )
        if task.get("delete_after_run"):
            delete_task(task_id)
        return

    # ── Multi-step prompt tasks ──────────────────────────────────────
    prompts = task["prompts"]
    steps = task["steps"]
    if not steps and not prompts:
        _emit_buddy_workflow_event(
            "done",
            task_id=task_id,
            thread_id=thread_id,
            label=task.get("name", "Workflow done"),
        )
        return
    # Use steps as the authoritative step list
    if not steps:
        steps = _prompts_to_steps(prompts)
    total = len(steps)
    if resume_run_id:
        run_id = resume_run_id
        _update_run_progress(run_id, start_step)
    else:
        run_id = _record_run_start(task_id, thread_id, total,
                                   task_name=task["name"], task_icon=task["icon"])

    def _run():
        from row_bot.agent import invoke_agent, TaskStoppedError
        from row_bot.threads import _save_thread_meta, _list_threads

        def _thread_exists(tid):
            return any(t[0] == tid for t in _list_threads())

        _stop_event = threading.Event()

        def _task_log(msg: str) -> None:
            """Append a log line visible to the Command Center UI."""
            with _active_lock:
                entry = _active_runs.get(thread_id)
                if entry and "log" in entry:
                    entry["log"].append(msg)

        with _active_lock:
            _active_runs[thread_id] = {
                "task_id": task_id,
                "run_id": run_id,
                "step": start_step,
                "total": total,
                "name": task["name"],
                "icon": task.get("icon", "⚡"),
                "started_at": datetime.now().isoformat(),
                "step_label": "",
                "log": [],
                "stop_event": _stop_event,
            }

        last_response = ""
        stopped = False
        paused = False  # set True when pausing for approval
        paused_message = "Waiting for approval"
        failure_message = ""
        step_outputs: dict[str, str] = resume_step_outputs.copy() if resume_step_outputs else {}
        # If resuming, seed last_response from the last step output
        if step_outputs:
            last_response = list(step_outputs.values())[-1]

        # Determine effective approval mode (block/approve/allow_all)
        try:
            runtime_policy = _workflow_profile_runtime_policy(task, enabled_tool_names)
        except Exception:
            logger.exception(
                "Task '%s' could not resolve Agent Profile policy; falling back to Default profile",
                task.get("name", ""),
            )
            fallback_task = {**task, "agent_profile_id": DEFAULT_WORKFLOW_AGENT_PROFILE_ID}
            runtime_policy = _workflow_profile_runtime_policy(fallback_task, enabled_tool_names)

        approval_mode = str(runtime_policy["approval_mode"])
        effective_tool_names = list(runtime_policy["effective_tool_names"])
        profile_snapshot = dict(runtime_policy["agent_profile_snapshot"])
        profile_skills = list(runtime_policy["skills_override"])
        tool_allowlist = list(runtime_policy["tool_allowlist"])

        from row_bot.threads import _set_thread_agent_profile, _set_thread_approval_mode, set_thread_skills_override

        _set_thread_agent_profile(thread_id, str(runtime_policy["agent_profile_id"]))
        _set_thread_approval_mode(thread_id, approval_mode)
        set_thread_skills_override(thread_id, profile_skills)

        if tool_allowlist:
            logger.info(
                "Task '%s' using Agent Profile tool allow-list - %d tool(s): %s",
                task["name"], len(effective_tool_names), effective_tool_names,
            )

        try:
            from row_bot.agent import _approval_mode_var, _background_workflow_var, _persistent_thread_var
            _background_workflow_var.set(True)
            _approval_mode_var.set(approval_mode)
            _persistent_thread_var.set(bool(task.get("persistent_thread_id")))

            config = {
                "configurable": {
                    "thread_id": thread_id,
                    "runtime_surface": "workflow",
                    "runtime_mode": "agent",
                    "approval_mode": approval_mode,
                    "agent_profile_id": str(runtime_policy["agent_profile_id"]),
                    "agent_profile_snapshot": profile_snapshot,
                },
            }
            if tool_allowlist:
                config["configurable"]["tool_allowlist"] = tool_allowlist

            def _format_interrupt_details(interrupts: list[dict]) -> list[str]:
                details = []
                for intr in interrupts:
                    tool_name = intr.get("tool", "unknown tool")
                    desc = intr.get("description", "")
                    details.append(desc or f"Tool '{tool_name}' needs approval")
                return details

            def _create_graph_interrupt_approval(step_id: str, approval_msg: str) -> tuple[str, str]:
                return create_approval_request(
                    run_id=run_id, task_id=task_id,
                    step_id=step_id, message=approval_msg,
                )

            def _save_graph_interrupt_state(
                current_step_index: int,
                current_step_outputs: dict,
                current_config: dict,
                resume_token: str,
            ) -> None:
                _save_pipeline_state(
                    run_id=run_id,
                    task_id=task_id,
                    thread_id=thread_id,
                    current_step_index=current_step_index,
                    step_outputs=current_step_outputs,
                    config=current_config,
                    resume_token=resume_token,
                    status="paused",
                    graph_interrupted=True,
                )

            def _notify_approval_channels(
                approval_id: str,
                resume_token: str,
                approval_msg: str,
            ) -> None:
                _push_approval_to_channels(
                    task, approval_id, resume_token, approval_msg,
                )

            # Model override
            if task.get("model_override"):
                config["configurable"]["model_override"] = task["model_override"]
                # Set on thread immediately so the UI shows the correct model
                # while the task is still running.
                from row_bot.threads import _set_thread_model_override
                _set_thread_model_override(thread_id, task["model_override"])

            step_index = start_step
            while step_index < total:
                step = steps[step_index]
                step_id = step.get("id", f"step_{step_index + 1}")
                step_type = step.get("type", "prompt")

                # ── Check stop before each step ──────────────────────
                if _stop_event.is_set():
                    stopped = True
                    logger.info("Task '%s' stopped before step %d/%d",
                                task["name"], step_index + 1, total)
                    break

                with _active_lock:
                    _active_runs[thread_id]["step"] = step_index

                # ── Dispatch by step type ────────────────────────────
                if step_type == "prompt":
                    prompt = step.get("prompt", "")
                    _step_label = (prompt[:60] + "…") if len(prompt) > 60 else prompt
                    with _active_lock:
                        _ar = _active_runs.get(thread_id)
                        if _ar:
                            _ar["step_label"] = _step_label
                    _task_log(f"▸ Step {step_index + 1}/{total}: {_step_label}")
                    try:
                        from row_bot.buddy.events import BuddyEventType, emit_buddy_event
                        emit_buddy_event(
                            BuddyEventType.WORKFLOW_STEP,
                            source="tasks",
                            payload={
                                "task_id": task_id,
                                "thread_id": thread_id,
                                "step": step_index + 1,
                                "total": total,
                                "label": _step_label,
                            },
                        )
                    except Exception:
                        logger.debug("Buddy workflow step event failed", exc_info=True)
                    # Apply per-step model override if present
                    step_model = step.get("model_override")
                    if step_model:
                        config["configurable"]["model_override"] = step_model
                    elif task.get("model_override"):
                        config["configurable"]["model_override"] = task["model_override"]

                    prompt = expand_template_vars(
                        prompt, task_id=task_id,
                        prev_output=last_response,
                        step_outputs=step_outputs,
                    )

                    # Per-step error recovery settings
                    step_on_error = step.get("on_error", "stop")
                    step_max_retries = step.get("max_retries", 1)
                    step_retry_delay = step.get("retry_delay_seconds", 5)
                    step_attempt = 0
                    step_succeeded = False

                    while step_attempt < step_max_retries:
                        step_attempt += 1
                        if step_attempt > 1:
                            import time as _time
                            logger.info(
                                "Task '%s' step %d: retry %d/%d (delay %ds)",
                                task["name"], step_index + 1,
                                step_attempt, step_max_retries, step_retry_delay,
                            )
                            _task_log(f"↻ Step {step_index + 1}: retry {step_attempt}/{step_max_retries}")
                            _time.sleep(step_retry_delay)
                            if _stop_event.is_set():
                                stopped = True
                                break
                        try:
                            generation_id = (
                                f"workflow:{run_id}:{step_id}:{step_attempt}"
                            )
                            config["configurable"]["generation_id"] = generation_id
                            config["configurable"]["root_objective"] = prompt
                            try:
                                result = invoke_agent(
                                    prompt,
                                    effective_tool_names,
                                    config,
                                    stop_event=_stop_event,
                                )
                            finally:
                                try:
                                    _record_run_recall_notices(run_id, generation_id)
                                except Exception:
                                    logger.debug(
                                        "Could not persist workflow memory fallback notice",
                                        exc_info=True,
                                    )
                            if not isinstance(result, dict):
                                from row_bot.agent_orchestrator import get_generation_orchestration

                                orchestration = get_generation_orchestration(
                                    thread_id,
                                    generation_id,
                                )
                                if (
                                    orchestration
                                    and int(orchestration.get("required_total") or 0) > 0
                                    and str(orchestration.get("parent_state") or "") == "waiting"
                                ):
                                    _save_pipeline_state(
                                        run_id=run_id,
                                        task_id=task_id,
                                        thread_id=thread_id,
                                        current_step_index=step_index,
                                        step_outputs=step_outputs,
                                        config=config,
                                        resume_token=f"orchestration:{orchestration['id']}",
                                        status=_CHILD_AGENT_WAIT_STATUS,
                                        graph_interrupted=_orchestration_wait_payload(
                                            str(orchestration["id"]),
                                            step_id,
                                        ),
                                    )
                                    paused = True
                                    paused_message = "Waiting for required child Agents"
                                    _task_log(
                                        f"Step {step_index + 1}: Waiting for required child Agents"
                                    )
                                    break
                            if isinstance(result, dict) and result.get("type") == "terminal":
                                terminal_reason = str(result.get("terminal_reason") or "")
                                message = str(result.get("message") or "Workflow step stopped incomplete.")
                                step_outputs[step_id] = message
                                _update_pipeline_status(run_id, "blocked")
                                _finish_run(
                                    run_id,
                                    "blocked",
                                    status_message=message,
                                    terminal_reason=terminal_reason,
                                )
                                _emit_buddy_workflow_event(
                                    "error",
                                    task_id=task_id,
                                    thread_id=thread_id,
                                    label="Workflow needs attention",
                                    error=message,
                                )
                                return
                            # ── Interrupt detection (approve mode) ───
                            if isinstance(result, dict) and result.get("type") == "interrupt":
                                # Check stop event first — avoid creating
                                # an approval for a task that was cancelled.
                                if _stop_event.is_set():
                                    stopped = True
                                    break

                                interrupts = result.get("interrupts", [])
                                if not interrupts:
                                    # Empty interrupt list — auto-continue
                                    logger.warning(
                                        "Task '%s' step %d: empty interrupt list — auto-continuing",
                                        task["name"], step_index + 1,
                                    )
                                    from row_bot.agent import resume_invoke_agent
                                    result = resume_invoke_agent(
                                        effective_tool_names, config,
                                        approved=True, stop_event=_stop_event,
                                    )
                                    if isinstance(result, str) and result:
                                        last_response = result
                                        step_outputs[step_id] = result
                                    step_succeeded = True
                                    break

                                # Safety-mode gate: only "approve" creates
                                # an approval request.  Block → refuse,
                                # allow_all → auto-resume.
                                if approval_mode == "block":
                                    logger.info(
                                        "Task '%s' step %d: interrupt in block mode — refusing",
                                        task["name"], step_index + 1,
                                    )
                                    from row_bot.agent import resume_invoke_agent
                                    result = resume_invoke_agent(
                                        effective_tool_names, config,
                                        approved=False, stop_event=_stop_event,
                                    )
                                    if isinstance(result, str) and result:
                                        last_response = result
                                        step_outputs[step_id] = result
                                    step_succeeded = True
                                    break

                                if approval_mode == "allow_all":
                                    logger.info(
                                        "Task '%s' step %d: interrupt in allow_all mode — auto-approving",
                                        task["name"], step_index + 1,
                                    )
                                    from row_bot.agent import resume_invoke_agent
                                    result = resume_invoke_agent(
                                        effective_tool_names, config,
                                        approved=True, stop_event=_stop_event,
                                    )
                                    if isinstance(result, str) and result:
                                        last_response = result
                                        step_outputs[step_id] = result
                                    step_succeeded = True
                                    break

                                # Build approval message from actual tool details
                                details = _format_interrupt_details(interrupts)
                                approval_msg = (
                                    f"Step {step_index + 1}/{total}: "
                                    + "; ".join(details)
                                )
                                resume_token, approval_req_id = _create_graph_interrupt_approval(
                                    step_id, approval_msg,
                                )
                                _save_graph_interrupt_state(
                                    step_index, step_outputs, config, resume_token,
                                )
                                paused = True
                                logger.info(
                                    "Task '%s' paused at step %d/%d — tool interrupt: %s",
                                    task["name"], step_index + 1, total, approval_msg,
                                )
                                _task_log(f"⏸ Step {step_index + 1}: Paused for approval")
                                _push_approval_to_channels(
                                    task, approval_req_id, resume_token, approval_msg,
                                )
                                if notification:
                                    from row_bot.notifications import notify
                                    notify(
                                        title="⏸️ Approval Required",
                                        message=f"{task['name']}: {approval_msg}",
                                        sound="workflow",
                                        icon="⏸️",
                                    )
                                break  # exit retry loop — approval will resume graph
                            if result:
                                last_response = result
                                step_outputs[step_id] = result
                            step_succeeded = True
                            _task_log(f"✓ Step {step_index + 1} complete")
                            break  # success — exit retry loop
                        except TaskStoppedError:
                            stopped = True
                            _task_log(f"⏹ Step {step_index + 1}: Stopped")
                            logger.info("Task '%s' stopped during step %d/%d",
                                        task["name"], step_index + 1, total)
                            break
                        except Exception as exc:
                            _task_log(f"✗ Step {step_index + 1} error: {str(exc)[:80]}")
                            err_str = str(exc).lower()
                            override = config["configurable"].get("model_override")
                            if (override
                                    and ("model failed to load" in err_str
                                         or "status code: 500" in err_str)):
                                failure_message = _workflow_model_failure_message(override, exc)
                                logger.error(
                                    "Task %s step %d explicit model override failed: %s",
                                    task["name"], step_index + 1, failure_message,
                                )
                                _task_log(f"✗ {failure_message[:160]}")
                            else:
                                logger.error(
                                    "Task %s step %d attempt %d failed: %s",
                                    task["name"], step_index + 1, step_attempt, exc,
                                )
                            try:
                                from row_bot.agent import repair_orphaned_tool_calls
                                repair_orphaned_tool_calls(effective_tool_names, config)
                            except Exception:
                                pass

                    if stopped:
                        break
                    if paused:
                        break  # interrupt-based approval — exit step loop
                    # Apply on_error policy if all retries exhausted
                    if not step_succeeded:
                        if step_on_error == "skip":
                            failure_message = ""
                            logger.info(
                                "Task '%s' step %d: on_error=skip — continuing",
                                task["name"], step_index + 1,
                            )
                        elif step_on_error == "stop":
                            logger.info(
                                "Task '%s' step %d: on_error=stop — halting pipeline",
                                task["name"], step_index + 1,
                            )
                            stopped = True
                            break

                # ── Approval step type ────────────────────────────────
                elif step_type == "approval":
                    approval_msg = step.get("message", f"Approval required to continue task '{task['name']}'")
                    approval_msg = expand_template_vars(
                        approval_msg, task_id=task_id,
                        prev_output=last_response,
                        step_outputs=step_outputs,
                    )
                    timeout_min = step.get("timeout_minutes", 30)
                    resume_token, approval_req_id = create_approval_request(
                        run_id=run_id,
                        task_id=task_id,
                        step_id=step_id,
                        message=approval_msg,
                        timeout_minutes=timeout_min,
                    )
                    # Save pipeline state for later resume
                    _save_pipeline_state(
                        run_id=run_id,
                        task_id=task_id,
                        thread_id=thread_id,
                        current_step_index=step_index,
                        step_outputs=step_outputs,
                        config=config,
                        resume_token=resume_token,
                        status="paused",
                    )
                    step_outputs[step_id] = approval_msg
                    paused = True
                    _task_log(f"⏸ Step {step_index + 1}: Waiting for approval")
                    logger.info(
                        "Task '%s' paused at step %d/%d for approval (token=%s…)",
                        task["name"], step_index + 1, total, resume_token[:8],
                    )
                    _notify_approval_channels(
                        approval_req_id, resume_token, approval_msg,
                    )
                    # Notify user an approval is pending
                    if notification:
                        from row_bot.notifications import notify
                        notify(
                            title="⏸️ Approval Required",
                            message=f"{task['name']}: {approval_msg}",
                            sound="workflow",
                            icon="⏸️",
                        )
                    break  # exit the step loop — resume will continue

                # ── Condition step type ────────────────────────────────
                elif step_type == "condition":
                    cond_expr = step.get("condition", "true")
                    cond_context = {
                        "prev_output": last_response,
                        "step_outputs": step_outputs,
                        "task_id": task_id,
                    }
                    result = evaluate_condition(cond_expr, cond_context)
                    target = step.get("if_true") if result else step.get("if_false")
                    logger.info(
                        "Task '%s' condition step %d: '%s' → %s → jump to %s",
                        task["name"], step_index + 1, cond_expr, result, target,
                    )
                    step_outputs[step_id] = str(result)
                    if target:
                        resolved = _resolve_step_index(steps, target)
                        if resolved is None:
                            # "end" — terminate pipeline
                            break
                        if resolved == step_index:
                            logger.error(
                                "Task '%s' condition step %d would jump to itself — stopping",
                                task["name"], step_index + 1,
                            )
                            break
                        step_index = resolved
                        _update_run_progress(run_id, step_index)
                        continue  # skip the step_index += 1 below
                    # No target specified — fall through to next step

                # ── Subtask step type ─────────────────────────────────
                elif step_type == "subtask":
                    child_task_id = step.get("task_id", "")
                    pass_output = step.get("pass_output", True)
                    child_task = get_task(child_task_id)
                    if not child_task:
                        logger.error(
                            "Task '%s' step %d: subtask '%s' not found — skipping",
                            task["name"], step_index + 1, child_task_id,
                        )
                    else:
                        current_depth = _subtask_depth_var.get()
                        if current_depth >= _MAX_SUBTASK_DEPTH:
                            logger.error(
                                "Task '%s' step %d: subtask depth limit (%d) reached — skipping",
                                task["name"], step_index + 1, _MAX_SUBTASK_DEPTH,
                            )
                        else:
                            child_result = _run_subtask_sync(
                                child_task, thread_id, effective_tool_names,
                                config, _stop_event,
                                parent_output=last_response if pass_output else "",
                                depth=current_depth + 1,
                            )
                            if child_result is not None:
                                last_response = child_result
                                step_outputs[step_id] = child_result
                            if _stop_event.is_set():
                                stopped = True
                                break

                # ── Notify step type ──────────────────────────────────
                elif step_type == "delegate_agent":
                    objective = expand_template_vars(
                        step.get("objective") or step.get("prompt", ""),
                        task_id=task_id,
                        prev_output=last_response,
                        step_outputs=step_outputs,
                    ).strip()
                    context = expand_template_vars(
                        step.get("context", ""),
                        task_id=task_id,
                        prev_output=last_response,
                        step_outputs=step_outputs,
                    ).strip()
                    if not objective:
                        failure_message = "Child Agent step is missing an objective."
                        logger.error(
                            "Task '%s' step %d: %s",
                            task["name"],
                            step_index + 1,
                            failure_message,
                        )
                        if step.get("on_error", "stop") == "skip":
                            step_outputs[step_id] = failure_message
                        else:
                            stopped = True
                            break
                    else:
                        child_run_id = ""
                        requested_worktree = (
                            bool(step.get("use_worktree"))
                            or str(step.get("editing_safety") or "").strip() == "worktree"
                            or str(step.get("workspace_mode") or "").strip() == "worktree"
                        )
                        try:
                            from row_bot.agent_runner import spawn_agent_run
                            from row_bot.threads import _get_thread_developer_workspace

                            profile_ref = str(
                                step.get("profile")
                                or step.get("agent_profile_id")
                                or "worker"
                            ).strip()
                            developer_workspace_id = str(
                                step.get("developer_workspace_id")
                                or _get_thread_developer_workspace(thread_id)
                                or ""
                            ).strip()
                            editing_safety = str(step.get("editing_safety") or "").strip()
                            use_worktree = bool(step.get("use_worktree")) or editing_safety == "worktree"
                            workspace_mode = str(step.get("workspace_mode") or "").strip()
                            if use_worktree and not workspace_mode:
                                workspace_mode = "worktree"
                            return_mode = str(step.get("return_mode") or "").strip().lower()
                            wait_for_result = bool(step.get("wait", True))
                            if return_mode in {"background", "start_in_background"}:
                                wait_for_result = False
                            _task_log(f"Step {step_index + 1}/{total}: Child Agent")
                            from row_bot.agent_orchestrator import create_or_get_orchestration
                            from row_bot.models import get_current_model

                            delegation_generation_id = (
                                f"workflow:{run_id}:{step_id}:delegate"
                            )
                            orchestration = create_or_get_orchestration(
                                parent_thread_id=thread_id,
                                parent_generation_id=delegation_generation_id,
                                parent_run_id=run_id,
                                root_objective=objective,
                                model_ref=str(
                                    step.get("model_override")
                                    or config["configurable"].get("model_override")
                                    or get_current_model()
                                ),
                                approval_mode=approval_mode,
                                runtime_surface="workflow",
                                delivery_context={
                                    "workflow_run_id": run_id,
                                    "workflow_step_id": step_id,
                                },
                                orchestration_version=2,
                            )
                            child_run = spawn_agent_run(
                                objective,
                                parent_thread_id=thread_id,
                                parent_run_id=run_id,
                                profile=profile_ref,
                                display_name=str(step.get("display_name") or ""),
                                context=context,
                                context_mode=str(step.get("context_mode") or "auto"),
                                enabled_tool_names=effective_tool_names,
                                model_override=str(step.get("model_override") or ""),
                                approval_mode=approval_mode,
                                developer_workspace_id=developer_workspace_id,
                                workspace_mode=workspace_mode,
                                use_worktree=use_worktree,
                                orchestration_id=str(orchestration.get("id") or ""),
                                orchestration_required=wait_for_result,
                                wait=False,
                            )
                            child_run_id = str((child_run or {}).get("id") or "")
                            if wait_for_result:
                                from row_bot.agent_orchestrator import arm_parent_wait
                                from row_bot.agent_runs import get_agent_run

                                child_run = get_agent_run(child_run_id) or child_run
                                child_output = _child_agent_output(child_run)
                                step_outputs[step_id] = json.dumps(
                                    child_output,
                                    sort_keys=True,
                                )
                                _save_pipeline_state(
                                    run_id=run_id,
                                    task_id=task_id,
                                    thread_id=thread_id,
                                    current_step_index=step_index,
                                    step_outputs=step_outputs,
                                    config=config,
                                    resume_token=f"orchestration:{orchestration['id']}",
                                    status=_CHILD_AGENT_WAIT_STATUS,
                                    graph_interrupted=_orchestration_wait_payload(
                                        str(orchestration["id"]),
                                        step_id,
                                    ),
                                )
                                arm_parent_wait(
                                    str(orchestration["id"]),
                                    continuation_state={
                                        "config": config,
                                        "enabled_tool_names": effective_tool_names,
                                        "workflow_run_id": run_id,
                                        "workflow_step_id": step_id,
                                        "workflow_direct_child": True,
                                    },
                                    delivery_context={
                                        "runtime_surface": "workflow",
                                        "workflow_run_id": run_id,
                                    },
                                )
                                paused = True
                                paused_message = (
                                    "Child Agent is waiting for approval"
                                    if child_output["status"] == "waiting_approval"
                                    else "Waiting for required child Agent"
                                )
                                _task_log(
                                    f"Step {step_index + 1}: Waiting for required child Agent"
                                )
                                break
                            child_status = str((child_run or {}).get("status") or "")
                            summary = str((child_run or {}).get("summary") or "")
                            child_output = _child_agent_output(child_run)
                            step_outputs[step_id] = json.dumps(child_output, sort_keys=True)
                            last_response = summary or step_outputs[step_id]
                            _task_log(
                                "Step "
                                f"{step_index + 1}: Agent {child_output['agent_run_id']} "
                                f"{child_status or 'started'}"
                            )
                        except Exception as exc:
                            failure_message = str(exc)
                            step_outputs[step_id] = failure_message
                            logger.error(
                                "Task '%s' step %d delegate Agent failed: %s",
                                task["name"],
                                step_index + 1,
                                exc,
                            )
                            _task_log(
                                f"Step {step_index + 1} delegate failed: {failure_message[:80]}"
                            )
                            if _is_child_agent_setup_failure(
                                exc,
                                child_run_id=child_run_id,
                                use_worktree=requested_worktree,
                            ):
                                stopped = True
                                break
                            if step.get("on_error", "stop") == "skip":
                                failure_message = ""
                            else:
                                stopped = True
                                break

                elif step_type == "wait_for_agents":
                    try:
                        run_ids: list[str] = []
                        raw_run_ids = step.get("run_ids") or []
                        if isinstance(raw_run_ids, str):
                            raw_run_ids = [
                                item.strip()
                                for item in raw_run_ids.split(",")
                                if item.strip()
                            ]
                        if isinstance(raw_run_ids, list):
                            run_ids.extend(
                                str(item).strip()
                                for item in raw_run_ids
                                if str(item).strip()
                            )
                        if not run_ids:
                            for raw_output in step_outputs.values():
                                try:
                                    parsed_output = json.loads(raw_output)
                                except Exception:
                                    continue
                                candidate = str(parsed_output.get("agent_run_id") or "").strip()
                                if candidate:
                                    run_ids.append(candidate)
                        seen_run_ids: set[str] = set()
                        run_ids = [
                            child_run_id
                            for child_run_id in run_ids
                            if not (child_run_id in seen_run_ids or seen_run_ids.add(child_run_id))
                        ]
                        if run_ids:
                            from row_bot.agent_orchestrator import (
                                arm_parent_wait,
                                create_or_get_orchestration,
                                get_member_for_run,
                                register_member,
                                transfer_member,
                            )
                            from row_bot.agent_runs import get_agent_run
                            from row_bot.models import get_current_model

                            orchestration = create_or_get_orchestration(
                                parent_thread_id=thread_id,
                                parent_generation_id=f"workflow:{run_id}:{step_id}:wait",
                                parent_run_id=run_id,
                                root_objective=f"Wait for {len(run_ids)} delegated Agent run(s).",
                                model_ref=str(
                                    config["configurable"].get("model_override")
                                    or get_current_model()
                                ),
                                approval_mode=approval_mode,
                                runtime_surface="workflow",
                                delivery_context={"workflow_run_id": run_id},
                                orchestration_version=2,
                            )
                            for child_run_id in run_ids:
                                existing_member = get_member_for_run(child_run_id)
                                if existing_member:
                                    transfer_member(
                                        str(orchestration["id"]),
                                        child_run_id,
                                        required=True,
                                    )
                                    continue
                                child_run = get_agent_run(child_run_id)
                                if not child_run:
                                    raise ValueError(
                                        f"Agent Run {child_run_id} was not found."
                                    )
                                register_member(
                                    str(orchestration["id"]),
                                    child_run_id,
                                    required=True,
                                )
                            step_outputs[step_id] = json.dumps(
                                {"agent_run_ids": run_ids},
                                sort_keys=True,
                            )
                            _save_pipeline_state(
                                run_id=run_id,
                                task_id=task_id,
                                thread_id=thread_id,
                                current_step_index=step_index,
                                step_outputs=step_outputs,
                                config=config,
                                resume_token=f"orchestration:{orchestration['id']}",
                                status=_CHILD_AGENT_WAIT_STATUS,
                                graph_interrupted=_orchestration_wait_payload(
                                    str(orchestration["id"]),
                                    step_id,
                                ),
                            )
                            arm_parent_wait(
                                str(orchestration["id"]),
                                continuation_state={
                                    "config": config,
                                    "enabled_tool_names": effective_tool_names,
                                    "workflow_run_id": run_id,
                                    "workflow_step_id": step_id,
                                    "workflow_direct_child": True,
                                },
                                delivery_context={
                                    "runtime_surface": "workflow",
                                    "workflow_run_id": run_id,
                                },
                            )
                            paused = True
                            paused_message = "Waiting for required child Agents"
                            _task_log(
                                f"Step {step_index + 1}: Waiting for {len(run_ids)} required Agent run(s)"
                            )
                            break
                    except Exception as exc:
                        failure_message = str(exc)
                        step_outputs[step_id] = failure_message
                        logger.error(
                            "Task '%s' step %d wait for Agents failed: %s",
                            task["name"],
                            step_index + 1,
                            exc,
                        )
                        if step.get("on_error", "stop") == "skip":
                            failure_message = ""
                        else:
                            stopped = True
                            break

                elif step_type == "notify":
                    notify_msg = step.get("message", "")
                    notify_msg = expand_template_vars(
                        notify_msg, task_id=task_id,
                        prev_output=last_response,
                        step_outputs=step_outputs,
                    )
                    notify_channel = step.get("channel", "desktop")
                    if notify_channel == "desktop":
                        from row_bot.notifications import notify as _notify
                        _notify(
                            title=f"📋 {task['name']}",
                            message=notify_msg,
                            sound="workflow",
                            icon="📋",
                        )
                    else:
                        # Use task's delivery channel mechanism
                        try:
                            _deliver_to_channel(
                                {**task, "delivery_channel": notify_channel},
                                notify_msg,
                            )
                        except Exception as exc:
                            logger.error(
                                "Task '%s' step %d notify failed: %s",
                                task["name"], step_index + 1, exc,
                            )
                    step_outputs[step_id] = notify_msg
                    last_response = notify_msg
                    logger.info(
                        "Task '%s' step %d: notify via %s",
                        task["name"], step_index + 1, notify_channel,
                    )

                # Unknown step types — log and skip
                else:
                    logger.warning(
                        "Task %s step %d: unknown step type '%s' — skipping",
                        task["name"], step_index + 1, step_type,
                    )

                # ── Optional "next" override on any step ─────────────
                next_target = step.get("next")
                if next_target:
                    resolved = _resolve_step_index(steps, next_target)
                    if resolved is None:
                        # "end" — terminate pipeline
                        break
                    step_index = resolved
                    _update_run_progress(run_id, step_index)
                    continue  # skip the default step_index += 1

                _update_run_progress(run_id, step_index + 1)
                step_index += 1

            # ── Handle stopped task ────────────────────────────────────
            if stopped:
                # Repair any orphaned tool calls left mid-step
                try:
                    from row_bot.agent import repair_orphaned_tool_calls
                    repair_orphaned_tool_calls(effective_tool_names, config)
                except Exception:
                    pass
                if failure_message:
                    _finish_run(run_id, "failed", status_message=failure_message)
                    _emit_buddy_workflow_event(
                        "error",
                        task_id=task_id,
                        thread_id=thread_id,
                        label="Workflow error",
                        error=failure_message,
                    )
                    if _thread_exists(thread_id):
                        thread_name = (f"âš¡ {task['name']} (failed) â€” "
                                       f"{datetime.now().strftime('%b %d, %I:%M %p')}")
                        _save_thread_meta(thread_id, thread_name)
                    return
                _finish_run(run_id, "stopped")
                _emit_buddy_workflow_event(
                    "cancelled",
                    task_id=task_id,
                    thread_id=thread_id,
                    label="Workflow stopped",
                )
                if _thread_exists(thread_id):
                    thread_name = (f"⚡ {task['name']} (stopped) — "
                                   f"{datetime.now().strftime('%b %d, %I:%M %p')}")
                    _save_thread_meta(thread_id, thread_name)
                if notification:
                    from row_bot.notifications import notify
                    notify(
                        title="⏹️ Task Stopped",
                        message=f"{task['name']} was stopped.",
                        sound="workflow",
                        icon="⏹️",
                    )
                return  # skip delivery, skip delete_after_run

            # ── Handle paused task (waiting for approval) ─────────────
            if paused:
                _finish_run(run_id, "paused",
                            status_message=paused_message)
                if _thread_exists(thread_id):
                    thread_name = (f"⚡ {task['name']} (paused) — "
                                   f"{datetime.now().strftime('%b %d, %I:%M %p')}")
                    _save_thread_meta(thread_id, thread_name)
                return  # the resume mechanism will continue execution

            # ── Determine final status ────────────────────────────────
            deliver_text = last_response or f"✅ Task '{task['name']}' completed."
            delivery_status, delivery_detail = _deliver_to_channels(
                task, deliver_text,
            )

            final_status = _workflow_final_status_for_delivery(delivery_status)
            _finish_run(run_id, final_status, status_message=delivery_detail)
            update_task(task_id, last_run=datetime.now().isoformat())
            logger.info("run_task_background: '%s' finished with status=%s",
                        task.get("name", "?"), final_status)
            _emit_buddy_workflow_event(
                "done",
                task_id=task_id,
                thread_id=thread_id,
                label=task.get("name", "Workflow done"),
            )

            # Fire any tasks triggered by this task's completion
            if final_status.startswith("completed"):
                try:
                    _fire_completion_triggers(task_id)
                except Exception as exc:
                    logger.error("Completion trigger error for %s: %s",
                                 task["name"], exc)

            # Thread naming (skip if thread was deleted while running)
            if _thread_exists(thread_id):
                thread_name = f"⚡ {task['name']} — {datetime.now().strftime('%b %d, %I:%M %p')}"
                _save_thread_meta(thread_id, thread_name)

            # Desktop + in-app notification (always)
            if notification:
                from row_bot.notifications import notify
                suffix = ""
                if delivery_status == "delivered":
                    suffix = f" → {delivery_detail}"
                elif delivery_status == "delivery_failed":
                    suffix = f" (⚠️ {delivery_detail})"
                notify(
                    title="⚡ Task Complete",
                    message=f"{task['name']} finished ({total} step{'s' if total != 1 else ''}).{suffix}",
                    sound="workflow",
                    icon="⚡",
                )
                if delivery_status == "delivery_failed":
                    notify(
                        title="⚠️ Delivery Failed",
                        message=f"{task['name']} — {delivery_detail}",
                        sound="timer",
                        icon="⚠️",
                    )

            # Auto-delete one-shot tasks
            if task.get("delete_after_run"):
                delete_task(task_id)

        except Exception as exc:
            logger.error("Task %s crashed: %s", task["name"], exc)
            _emit_buddy_workflow_event(
                "error",
                task_id=task_id,
                thread_id=thread_id,
                label="Workflow error",
                error=str(exc),
            )
            _finish_run(run_id, "failed", status_message=str(exc))
        finally:
            with _active_lock:
                _active_runs.pop(thread_id, None)
            # Release the browser tab owned by this thread (if any)
            try:
                from row_bot.tools.browser_tool import get_session_manager as _get_bsm
                _get_bsm().kill_session(thread_id)
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True, name=f"task-{task_id}")
    t.start()


# ── Scheduler (APScheduler) ──────────────────────────────────────────────────
# Each enabled task gets a real APScheduler job with the appropriate trigger
# (CronTrigger, IntervalTrigger, DateTrigger).  Adding/updating/deleting a
# task automatically syncs the scheduler.

_scheduler: "BackgroundScheduler | None" = None
_scheduler_lock = threading.Lock()


def _get_scheduler():
    """Return the singleton BackgroundScheduler, creating it if needed."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
        )
        _scheduler.start()
        logger.info("APScheduler BackgroundScheduler started")
    return _scheduler


def _parse_schedule(schedule: str | None) -> dict | None:
    """Parse schedule strings into a dict.

    Formats:
        "daily:HH:MM"             → run every day at HH:MM
        "weekly:DAY:HH:MM"        → run every week on DAY at HH:MM
        "interval:HOURS"           → run every N hours (float OK, e.g. 0.5 = 30 min)
        "interval_minutes:MINUTES" → run every N minutes
        "cron:EXPR"                → cron expression (5-field)
    """
    if not schedule:
        return None
    parts = schedule.split(":", 1)
    if len(parts) < 2:
        return None

    kind = parts[0].lower()
    rest = parts[1]

    try:
        if kind == "daily":
            sub = rest.split(":")
            if len(sub) >= 2:
                return {"kind": "daily", "hour": int(sub[0]), "minute": int(sub[1])}
        elif kind == "weekly":
            sub = rest.split(":")
            if len(sub) >= 3:
                raw_day = sub[0].lower()
                _FULL_TO_ABBR = {
                    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
                    "thursday": "thu", "friday": "fri", "saturday": "sat",
                    "sunday": "sun",
                }
                day_abbr = _FULL_TO_ABBR.get(raw_day, raw_day)
                return {
                    "kind": "weekly",
                    "day": day_abbr,
                    "hour": int(sub[1]),
                    "minute": int(sub[2]),
                }
        elif kind == "interval":
            return {"kind": "interval", "hours": float(rest)}
        elif kind == "interval_minutes":
            return {"kind": "interval_minutes", "minutes": float(rest)}
        elif kind == "cron":
            return {"kind": "cron", "expr": rest.strip()}
    except (ValueError, IndexError):
        pass
    return None


_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Reverse map: weekday int → 3-letter APScheduler day string
_DAY_TO_AP = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _build_trigger(task: dict):
    """Build an APScheduler trigger from a task's schedule/at fields.

    Returns a trigger object or *None* if the task has no valid schedule.
    """
    # One-shot ``at`` tasks — fire once at a specific datetime
    at_str = task.get("at")
    if at_str:
        try:
            from apscheduler.triggers.date import DateTrigger
            at_dt = datetime.fromisoformat(at_str)
            # Only schedule if in the future
            if at_dt > datetime.now():
                return DateTrigger(run_date=at_dt)
            # Already past — check if it already fired
            lr = task.get("last_run")
            if lr:
                try:
                    if datetime.fromisoformat(lr) >= at_dt:
                        return None  # Already fired
                except (ValueError, TypeError):
                    pass
            # Past but never fired — fire immediately
            return DateTrigger(run_date=datetime.now() + timedelta(seconds=2))
        except (ValueError, TypeError):
            pass
        return None

    sched = _parse_schedule(task.get("schedule"))
    if not sched:
        return None

    kind = sched["kind"]

    if kind == "daily":
        from apscheduler.triggers.cron import CronTrigger
        return CronTrigger(hour=sched["hour"], minute=sched["minute"])

    elif kind == "weekly":
        from apscheduler.triggers.cron import CronTrigger
        day_int = _DAY_MAP.get(sched["day"])
        if day_int is None:
            return None
        ap_day = _DAY_TO_AP[day_int]
        return CronTrigger(day_of_week=ap_day, hour=sched["hour"], minute=sched["minute"])

    elif kind == "interval":
        from apscheduler.triggers.interval import IntervalTrigger
        hours = sched["hours"]
        if hours <= 0:
            return None
        return IntervalTrigger(hours=hours)

    elif kind == "interval_minutes":
        from apscheduler.triggers.interval import IntervalTrigger
        minutes = sched["minutes"]
        if minutes <= 0:
            return None
        return IntervalTrigger(minutes=int(minutes))

    elif kind == "cron":
        try:
            from apscheduler.triggers.cron import CronTrigger
            return CronTrigger.from_crontab(sched["expr"])
        except Exception:
            logger.warning("Invalid cron expression: %s", sched["expr"])
            return None

    return None


def _job_id(task_id: str) -> str:
    """Deterministic APScheduler job ID for a task."""
    return f"task_{task_id}"


def _prepare_task_thread(task: dict) -> str:
    """Canonical thread setup for firing a task.

    Returns the thread_id to use.  Handles:
    - persistent_thread_id (reuse) vs fresh UUID
    - thread_meta creation
    - model_override propagation
    """
    from row_bot.threads import (
        _save_thread_meta,
        _set_thread_agent_profile,
        _set_thread_approval_mode,
        _set_thread_model_override,
    )

    thread_id = task.get("persistent_thread_id") or uuid.uuid4().hex[:12]
    if not task.get("notify_only"):
        thread_name = (
            f"\u26a1 {task['name']} \u2014 "
            f"{datetime.now().strftime('%b %d, %I:%M %p')}"
        )
        _save_thread_meta(thread_id, thread_name)
    if task.get("model_override"):
        _set_thread_model_override(thread_id, task["model_override"])
    try:
        _set_thread_agent_profile(thread_id, _workflow_agent_profile_ref(task))
    except Exception:
        logger.debug("Could not set workflow thread Agent Profile", exc_info=True)
    _set_thread_approval_mode(thread_id, get_task_approval_mode(task))
    return thread_id


def _on_task_fire(task_id: str) -> None:
    """Callback invoked by APScheduler when a task's trigger fires."""
    from row_bot.tools import registry as tool_registry

    task = get_task(task_id)
    if not task:
        return
    if not task.get("enabled", True):
        return

    logger.info("Scheduler firing task: %s", task["name"])
    update_task(task_id, last_run=datetime.now().isoformat())

    thread_id = _prepare_task_thread(task)
    enabled = [t.name for t in tool_registry.get_enabled_tools()]

    run_task_background(task_id, thread_id, enabled, notification=True)

    # Auto-remove one-shot `at` tasks after firing
    if task.get("at") and task.get("delete_after_run"):
        _remove_job(task_id)


def _sync_job(task: dict) -> None:
    """Add or update the APScheduler job for a single task."""
    scheduler = _get_scheduler()
    jid = _job_id(task["id"])

    if not task.get("enabled", True):
        # Disabled — remove job if it exists
        try:
            scheduler.remove_job(jid)
        except Exception:
            pass
        return

    trigger = _build_trigger(task)
    if trigger is None:
        # No valid schedule — remove any leftover job
        try:
            scheduler.remove_job(jid)
        except Exception:
            pass
        return

    # Add or replace the job
    scheduler.add_job(
        _on_task_fire,
        trigger=trigger,
        args=[task["id"]],
        id=jid,
        name=task["name"],
        replace_existing=True,
    )


def _remove_job(task_id: str) -> None:
    """Remove the APScheduler job for a task (if it exists)."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(task_id))
    except Exception:
        pass


@_schema_retry
def sync_all_jobs() -> None:
    """(Re-)sync every task to the APScheduler job store."""
    tasks = list_tasks()
    for task in tasks:
        _sync_job(task)
    logger.info("Synced %d task(s) to APScheduler", len(tasks))


@_schema_retry
def start_task_scheduler() -> None:
    """Start the APScheduler and sync all task jobs (idempotent)."""
    _get_scheduler()
    sync_all_jobs()
    start_approval_monitor()


# Backward-compat alias
start_workflow_scheduler = start_task_scheduler


# ── Webhook Trigger ──────────────────────────────────────────────────────────

def handle_webhook(task_id: str, secret: str | None = None,
                   payload: dict | None = None) -> dict:
    """Handle an incoming webhook trigger for a task.

    Returns a dict with ``status`` and ``message`` keys.
    """
    task = get_task(task_id)
    if not task:
        return {"status": "error", "message": "Task not found"}

    trigger = task.get("trigger")
    if not trigger or trigger.get("type") != "webhook":
        return {"status": "error", "message": "Task does not have a webhook trigger"}

    # Validate secret
    expected_secret = trigger.get("secret", "")
    if expected_secret and expected_secret != secret:
        return {"status": "error", "message": "Invalid secret"}

    if not task.get("enabled", True):
        return {"status": "error", "message": "Task is disabled"}

    from row_bot.tools import registry as tool_registry

    thread_id = _prepare_task_thread(task)
    enabled = [t.name for t in tool_registry.get_enabled_tools()]

    run_task_background(task_id, thread_id, enabled)
    logger.info("Webhook triggered task '%s'", task["name"])
    return {"status": "ok", "message": f"Task '{task['name']}' triggered"}


def generate_webhook_secret() -> str:
    """Generate a random webhook secret."""
    import secrets
    return secrets.token_urlsafe(24)


# ── Concurrency Groups ──────────────────────────────────────────────────────

def get_concurrency_group(task: dict) -> str | None:
    """Return the effective concurrency group for a task.

    Auto-assigns 'local_gpu' for tasks using local models.
    """
    explicit = task.get("concurrency_group")
    if explicit:
        return explicit

    # Auto-detect local model usage
    model = task.get("model_override", "")
    if model and any(local in model.lower() for local in
                     ("ollama", "llama", "local", "lmstudio", "localhost")):
        return "local_gpu"
    return None


def check_concurrency_group(task: dict) -> bool:
    """Check if a task can run given its concurrency group.

    Returns True if the task can proceed, False if another task in the
    same group is already running.
    """
    group = get_concurrency_group(task)
    if not group:
        return True  # no group — always allowed

    with _active_lock:
        for tid, info in _active_runs.items():
            if tid == task.get("persistent_thread_id"):
                continue  # same task re-running
            running_task = get_task(info.get("task_id", ""))
            if running_task and get_concurrency_group(running_task) == group:
                logger.info(
                    "Concurrency group '%s': task '%s' blocked by running '%s'",
                    group, task["name"], running_task["name"],
                )
                return False
    return True


# ── Global Retry Config ──────────────────────────────────────────────────────

_RETRY_CONFIG_PATH = _TASK_CONFIG_PATH


def get_retry_max() -> int:
    """Return the global max retries (default 1 = no retry)."""
    try:
        with open(_RETRY_CONFIG_PATH) as f:
            return json.load(f).get("retry_max", 1)
    except (FileNotFoundError, json.JSONDecodeError):
        return 1


def set_retry_max(value: int) -> None:
    """Save the global retry setting."""
    data = {}
    try:
        with open(_RETRY_CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    data["retry_max"] = max(1, value)
    with open(_RETRY_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── Global Safety Mode ──────────────────────────────────────────────────────

_VALID_APPROVAL_MODES = ("block", "approve", "allow_all")
_VALID_SAFETY_MODES = _VALID_APPROVAL_MODES


def get_global_approval_mode() -> str:
    """Return the global default approval mode for new tasks."""
    try:
        with open(_RETRY_CONFIG_PATH) as f:
            mode = json.load(f).get("safety_mode", "block")
            return legacy_safety_mode_to_approval_mode(mode)
    except (FileNotFoundError, json.JSONDecodeError):
        return "block"


def set_global_approval_mode(mode: str) -> None:
    """Save the global approval mode setting."""
    normalized = normalize_approval_mode(mode, "")
    if normalized not in _VALID_APPROVAL_MODES:
        raise ValueError(f"Invalid approval mode: {mode!r}. Must be one of {_VALID_APPROVAL_MODES}")
    data = {}
    try:
        with open(_RETRY_CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    data["safety_mode"] = normalized
    with open(_RETRY_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_task_approval_mode(task: dict) -> str:
    """Return the effective approval mode for a task.

    Uses the task's own setting if present, otherwise falls back to
    the global default.
    """
    mode = task.get("safety_mode")
    if mode:
        return legacy_safety_mode_to_approval_mode(mode)
    return get_global_approval_mode()


def get_global_safety_mode() -> str:
    """Compatibility alias for old workflow settings."""
    return get_global_approval_mode()


def set_global_safety_mode(mode: str) -> None:
    """Compatibility alias for old workflow settings."""
    set_global_approval_mode(mode)


def get_task_safety_mode(task: dict) -> str:
    """Compatibility alias for old workflow settings."""
    return get_task_approval_mode(task)


# ── Smart Tool Selection (auto-inference engine) ────────────────────────────

# Stop words excluded from keyword extraction (common English words that
# would cause false matches against every prompt).
_INFERENCE_STOP_WORDS: set[str] = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "as", "be", "was", "are",
    "this", "that", "can", "will", "do", "not", "all", "use", "your",
    "you", "we", "they", "he", "she", "has", "have", "had", "been",
    "would", "could", "should", "may", "might", "if", "then", "else",
    "so", "no", "yes", "up", "out", "about", "into", "over", "after",
    "also", "just", "more", "than", "very", "too", "any", "each",
    "its", "my", "our", "their", "which", "what", "when", "where",
    "how", "who", "whom", "these", "those", "get", "set", "new",
    "one", "two", "run", "tool", "tools", "agent", "data", "using",
    "given", "take", "make", "like", "such", "via", "etc", "e.g",
    "based", "i.e", "provide", "return", "result", "results",
    "input", "output", "name", "value", "type", "list",
}

# Cached keyword map: tool_name → set of keywords.  Invalidated when
# the tool registry changes.
_keyword_map_cache: dict[str, set[str]] | None = None


def _build_keyword_map() -> dict[str, set[str]]:
    """Build a mapping from tool name → set of lowercase keywords.

    Keywords are extracted from:
      - tool.name (split on underscores)
      - tool.display_name (split on whitespace, stripped of emoji)
      - tool.description (split on whitespace, stop-words removed)
      - sub-tool names from as_langchain_tools() (split on underscores)
      - tool.inference_keywords (explicit additions from subclasses)
    """
    from row_bot.tools import registry as tool_registry

    kw_map: dict[str, set[str]] = {}
    for tool in tool_registry.get_all_tools():
        keywords: set[str] = set()

        # Name parts  (e.g. "web_search" → {"web", "search"})
        keywords.update(tool.name.lower().split("_"))

        # Display name (strip emoji via ASCII filter, split)
        display_clean = "".join(
            ch for ch in tool.display_name if ch.isascii()
        ).strip()
        keywords.update(w.lower() for w in display_clean.split() if len(w) > 1)

        # Description words (stop-word filtered)
        for word in tool.description.lower().split():
            # Strip punctuation
            clean = "".join(ch for ch in word if ch.isalnum())
            if clean and len(clean) > 2 and clean not in _INFERENCE_STOP_WORDS:
                keywords.add(clean)

        # Sub-tool names  (e.g. "send_gmail_message" → {"send", "gmail", "message"})
        try:
            for lc_tool in tool.as_langchain_tools():
                keywords.update(lc_tool.name.lower().split("_"))
        except Exception:
            pass  # Some tools may fail if deps/keys are missing

        # Explicit inference keywords from the tool subclass
        keywords.update(kw.lower() for kw in tool.inference_keywords)

        # Remove stop words and very short tokens
        keywords = {
            kw for kw in keywords
            if kw not in _INFERENCE_STOP_WORDS and len(kw) > 1
        }

        kw_map[tool.name] = keywords

    return kw_map


def _get_keyword_map() -> dict[str, set[str]]:
    """Return the cached keyword map, rebuilding if necessary."""
    global _keyword_map_cache
    if _keyword_map_cache is None:
        _keyword_map_cache = _build_keyword_map()
    return _keyword_map_cache


def invalidate_keyword_map_cache() -> None:
    """Clear the keyword map cache (call when tools are added/removed)."""
    global _keyword_map_cache
    _keyword_map_cache = None


# Tools that are always included regardless of inference results —
# the agent needs these for core functionality.
_ALWAYS_INCLUDE_TOOLS: set[str] = {"conversation_search", "memory"}


def infer_tools_for_prompt(
    prompts: list[str],
    available_tool_names: list[str],
) -> list[str]:
    """Score each tool against the prompt texts and return matching tool names.

    *prompts* is a list of prompt/step text strings from the task.
    *available_tool_names* are the currently enabled tool names.

    Returns a subset of *available_tool_names* that match, plus always-on
    tools.  If no tools match, returns *available_tool_names* unchanged
    (safe fallback).
    """
    if not prompts:
        return list(available_tool_names)

    kw_map = _get_keyword_map()

    # Tokenize all prompts into a single set of lowercase words
    prompt_words: set[str] = set()
    for p in prompts:
        for word in p.lower().split():
            clean = "".join(ch for ch in word if ch.isalnum())
            if clean and len(clean) > 1:
                prompt_words.add(clean)

    available_set = set(available_tool_names)
    matched: set[str] = set()

    for tool_name, keywords in kw_map.items():
        if tool_name not in available_set:
            continue
        hits = keywords & prompt_words
        if hits:
            matched.add(tool_name)

    if not matched:
        # No keyword matches at all — fall back to all tools
        return list(available_tool_names)

    # Always include core tools if they're available
    matched |= _ALWAYS_INCLUDE_TOOLS & available_set

    return sorted(matched)





# ── Pipeline State Persistence (for approval pause/resume) ──────────────────

@_schema_retry
def _save_pipeline_state(
    run_id: str,
    task_id: str,
    thread_id: str,
    current_step_index: int,
    step_outputs: dict,
    config: dict,
    resume_token: str | None = None,
    status: str = "running",
    graph_interrupted: bool | str = False,
) -> None:
    """Persist pipeline state to the DB for later resumption."""
    conn = _get_conn()
    now = datetime.now().isoformat()
    extra_cols = ""
    extra_placeholders = ""
    extra_vals: list = []
    if status == "paused":
        extra_cols += ", paused_at"
        extra_placeholders += ", ?"
        extra_vals.append(now)
    if graph_interrupted:
        extra_cols += ", graph_interrupted"
        extra_placeholders += ", ?"
        extra_vals.append("true" if graph_interrupted is True else str(graph_interrupted))
    conn.execute(
        "INSERT OR REPLACE INTO pipeline_state "
        "(run_id, task_id, thread_id, current_step_index, step_outputs, "
        "status, resume_token, config, created_at, updated_at"
        + extra_cols + ") "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
        + extra_placeholders + ")",
        (
            run_id, task_id, thread_id, current_step_index,
            json.dumps(step_outputs), status, resume_token,
            json.dumps(config, default=str), now, now,
            *extra_vals,
        ),
    )
    conn.commit()
    conn.close()


@_schema_retry
def _load_pipeline_state(resume_token: str) -> dict | None:
    """Load pipeline state by resume token."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM pipeline_state WHERE resume_token = ?",
        (resume_token,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["step_outputs"] = json.loads(d.get("step_outputs") or "{}")
    d["config"] = json.loads(d.get("config") or "{}")
    return d


@_schema_retry
def _update_pipeline_status(run_id: str, status: str) -> None:
    """Update the status of a pipeline state entry."""
    conn = _get_conn()
    conn.execute(
        "UPDATE pipeline_state SET status = ?, updated_at = ? WHERE run_id = ?",
        (status, datetime.now().isoformat(), run_id),
    )
    conn.commit()
    conn.close()


@_schema_retry
def _clear_graph_interrupted(run_id: str) -> None:
    """Clear the graph_interrupted flag after a successful resume."""
    conn = _get_conn()
    conn.execute(
        "UPDATE pipeline_state SET graph_interrupted = NULL, updated_at = ? "
        "WHERE run_id = ?",
        (datetime.now().isoformat(), run_id),
    )
    conn.commit()
    conn.close()


_CHILD_AGENT_WAIT_STATUS = "waiting_child_agent"
_CHILD_AGENT_WAIT_TYPE = "child_agent_wait"
_ORCHESTRATION_WAIT_TYPE = "agent_orchestration_wait"
_CHILD_AGENT_SUCCESS_STATUSES = {"completed", "completed_delivery_failed"}
_CHILD_AGENT_STOP_STATUSES = {"stopped", "cancelled"}
_CHILD_AGENT_SETUP_FAILURE_FRAGMENTS = (
    "Worktree requires a git-backed Developer workspace",
    "Worktree requires a git repository",
    "Worktree requires a git repository root",
    "Cannot create Worktree:",
    "Failed to create Worktree",
    "Worktree did not return a usable workspace",
)


def _is_child_agent_setup_failure(
    exc: Exception,
    *,
    child_run_id: str = "",
    use_worktree: bool = False,
) -> bool:
    """Return whether a delegate failure happened before a usable child run."""
    if child_run_id:
        return False
    try:
        from row_bot.agent_runner import AgentRunnerError

        if isinstance(exc, AgentRunnerError):
            return True
    except Exception:
        if exc.__class__.__name__ == "AgentRunnerError":
            return True
    if use_worktree:
        message = str(exc)
        return any(fragment in message for fragment in _CHILD_AGENT_SETUP_FAILURE_FRAGMENTS)
    return False


def _child_agent_output(run: dict | None) -> dict[str, str]:
    return {
        "agent_run_id": str((run or {}).get("id") or ""),
        "status": str((run or {}).get("status") or ""),
        "summary": str((run or {}).get("summary") or ""),
        "thread_id": str((run or {}).get("thread_id") or ""),
        "workspace_id": str((run or {}).get("workspace_id") or ""),
        "workspace_path": str((run or {}).get("workspace_path") or ""),
        "workspace_mode": str((run or {}).get("workspace_mode") or ""),
    }


def _child_agent_failure_message(child_output: dict[str, str]) -> str:
    run_id = child_output.get("agent_run_id") or "unknown"
    status = child_output.get("status") or "unknown"
    return f"Child Agent {run_id} finished with status: {status}."


def _child_agent_parent_terminal_status(child_status: str) -> str:
    if child_status in _CHILD_AGENT_STOP_STATUSES:
        return "stopped"
    return "failed"


def _child_agent_wait_payload(child_run_id: str, step_id: str) -> str:
    return json.dumps(
        {
            "type": _CHILD_AGENT_WAIT_TYPE,
            "child_agent_run_id": child_run_id,
            "step_id": step_id,
        },
        sort_keys=True,
    )


def _orchestration_wait_payload(orchestration_id: str, step_id: str) -> str:
    return json.dumps(
        {
            "type": _ORCHESTRATION_WAIT_TYPE,
            "orchestration_id": orchestration_id,
            "step_id": step_id,
        },
        sort_keys=True,
    )


def _parse_orchestration_wait_payload(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("type") != _ORCHESTRATION_WAIT_TYPE:
        return None
    return {
        "orchestration_id": str(payload.get("orchestration_id") or ""),
        "step_id": str(payload.get("step_id") or ""),
    }


def _parse_child_agent_wait_payload(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("type") != _CHILD_AGENT_WAIT_TYPE:
        return None
    return {
        "child_agent_run_id": str(payload.get("child_agent_run_id") or ""),
        "step_id": str(payload.get("step_id") or ""),
    }


@_schema_retry
def _load_pipeline_states_waiting_for_child_agent(child_run_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM pipeline_state WHERE status = ?",
        (_CHILD_AGENT_WAIT_STATUS,),
    ).fetchall()
    conn.close()
    states: list[dict] = []
    for row in rows:
        state = dict(row)
        payload = _parse_child_agent_wait_payload(state.get("graph_interrupted"))
        if not payload or payload.get("child_agent_run_id") != child_run_id:
            continue
        state["step_outputs"] = json.loads(state.get("step_outputs") or "{}")
        state["config"] = json.loads(state.get("config") or "{}")
        state["child_agent_wait"] = payload
        states.append(state)
    return states


@_schema_retry
def _claim_child_agent_wait_state(run_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE pipeline_state SET status = ?, updated_at = ? "
            "WHERE run_id = ? AND status = ?",
            (
                "running",
                datetime.now().isoformat(),
                run_id,
                _CHILD_AGENT_WAIT_STATUS,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


@_schema_retry
def _load_pipeline_states_waiting_for_orchestration(
    orchestration_id: str,
) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM pipeline_state WHERE status = ?",
        (_CHILD_AGENT_WAIT_STATUS,),
    ).fetchall()
    conn.close()
    states: list[dict] = []
    for row in rows:
        state = dict(row)
        payload = _parse_orchestration_wait_payload(state.get("graph_interrupted"))
        if not payload or payload.get("orchestration_id") != orchestration_id:
            continue
        state["step_outputs"] = json.loads(state.get("step_outputs") or "{}")
        state["config"] = json.loads(state.get("config") or "{}")
        state["orchestration_wait"] = payload
        states.append(state)
    return states


def resume_workflows_waiting_for_orchestration(orchestration_id: str) -> int:
    """Resume workflow steps only after their durable required barrier."""

    if not orchestration_id:
        return 0
    from row_bot.agent_orchestrator import (
        list_members,
        list_messages,
        get_orchestration,
    )
    from row_bot.tools import registry as tool_registry

    orchestration = get_orchestration(orchestration_id)
    if not orchestration or orchestration.get("status") not in {
        "completed",
        "completed_partial",
        "failed",
        "stopped",
    }:
        return 0
    final_messages = list_messages(
        orchestration_id,
        kinds=["parent_final", "final"],
    )
    final_text = str(final_messages[-1].get("content") or "") if final_messages else ""
    continuation = orchestration.get("continuation_state_json") or {}
    members = [
        member for member in list_members(orchestration_id) if member.get("required")
    ]
    failed_members = [
        member
        for member in members
        if str(member.get("status") or "")
        in {"failed", "stopped", "blocked", "timed_out", "cancelled"}
    ]
    resumed = 0
    for state in _load_pipeline_states_waiting_for_orchestration(orchestration_id):
        run_id = str(state.get("run_id") or "")
        if not run_id or not _claim_child_agent_wait_state(run_id):
            continue
        task_id = str(state.get("task_id") or "")
        task = get_task(task_id)
        if not task:
            _finish_run(run_id, "failed", status_message=f"Task {task_id} not found")
            continue
        steps = task.get("steps") or []
        step_index = int(state.get("current_step_index") or 0)
        step = steps[step_index] if step_index < len(steps) else {}
        step_id = str(
            (state.get("orchestration_wait") or {}).get("step_id")
            or step.get("id")
            or f"step_{step_index + 1}"
        )
        step_outputs = dict(state.get("step_outputs") or {})
        step_outputs[step_id] = final_text
        if (
            failed_members
            and str(step.get("type") or "prompt") == "delegate_agent"
            and str(step.get("on_error") or "stop") != "skip"
        ):
            failed_run = (failed_members[0].get("run") or {}) if failed_members else {}
            message = _child_agent_failure_message(_child_agent_output(failed_run))
            if final_text:
                message = f"{message} {final_text}"
            parent_status = (
                "stopped"
                if all(
                    str(member.get("status") or "") in _CHILD_AGENT_STOP_STATUSES
                    for member in failed_members
                )
                else "failed"
            )
            _update_pipeline_status(run_id, parent_status)
            _finish_run(run_id, parent_status, status_message=message)
            resumed += 1
            continue
        _clear_graph_interrupted(run_id)
        if "enabled_tool_names" in continuation:
            enabled = [
                str(name)
                for name in (continuation.get("enabled_tool_names") or [])
                if str(name)
            ]
        else:
            # Compatibility for pre-orchestration pipeline rows only.
            enabled = [tool.name for tool in tool_registry.get_enabled_tools()]
        run_task_background(
            task_id,
            str(state.get("thread_id") or ""),
            enabled,
            start_step=step_index + 1,
            notification=True,
            resume_step_outputs=step_outputs,
            resume_run_id=run_id,
        )
        resumed += 1
    return resumed


def resume_workflows_waiting_for_child_agent(child_run_id: str) -> int:
    """Resume paused workflows whose waited child Agent has reached terminal."""
    if not child_run_id:
        return 0
    from row_bot.agent_runner import agent_run_is_terminal
    from row_bot.agent_runs import get_agent_run
    from row_bot.tools import registry as tool_registry
    from row_bot.threads import _list_threads, _save_thread_meta

    child_run = get_agent_run(child_run_id)
    if not agent_run_is_terminal(child_run):
        return 0
    child_output = _child_agent_output(child_run)
    child_status = child_output["status"]
    resumed = 0
    for state in _load_pipeline_states_waiting_for_child_agent(child_run_id):
        run_id = str(state.get("run_id") or "")
        if not run_id or not _claim_child_agent_wait_state(run_id):
            continue
        task_id = str(state.get("task_id") or "")
        task = get_task(task_id)
        if not task:
            _finish_run(run_id, "failed", status_message=f"Task {task_id} not found")
            continue
        thread_id = str(state.get("thread_id") or "")
        steps = task.get("steps") or []
        step_index = int(state.get("current_step_index") or 0)
        step = steps[step_index] if step_index < len(steps) else {}
        step_id = str(
            (state.get("child_agent_wait") or {}).get("step_id")
            or step.get("id")
            or f"step_{step_index + 1}"
        )
        step_outputs = dict(state.get("step_outputs") or {})
        step_outputs[step_id] = json.dumps(child_output, sort_keys=True)
        if child_status not in _CHILD_AGENT_SUCCESS_STATUSES:
            parent_status = _child_agent_parent_terminal_status(child_status)
            message = _child_agent_failure_message(child_output)
            _update_pipeline_status(run_id, parent_status)
            _finish_run(run_id, parent_status, status_message=message)
            _emit_buddy_workflow_event(
                "cancelled" if parent_status == "stopped" else "error",
                task_id=task_id,
                thread_id=thread_id,
                label="Workflow stopped" if parent_status == "stopped" else "Workflow error",
                error=message,
            )
            if any(t[0] == thread_id for t in _list_threads()):
                suffix = "stopped" if parent_status == "stopped" else "failed"
                thread_name = (
                    f"{task['name']} ({suffix}) - "
                    f"{datetime.now().strftime('%b %d, %I:%M %p')}"
                )
                _save_thread_meta(thread_id, thread_name)
            resumed += 1
            continue
        _clear_graph_interrupted(run_id)
        enabled = [t.name for t in tool_registry.get_enabled_tools()]
        run_task_background(
            task_id,
            thread_id,
            enabled,
            start_step=step_index + 1,
            notification=True,
            resume_step_outputs=step_outputs,
            resume_run_id=run_id,
        )
        resumed += 1
    return resumed


# ── Approval Request Management ─────────────────────────────────────────────

@_schema_retry
def create_approval_request(
    run_id: str,
    task_id: str,
    step_id: str,
    message: str,
    channel: str | None = None,
    timeout_minutes: int = 30,
    agent_run_id: str = "",
    resume_kind: str = "",
    source_label: str = "",
    source_thread_id: str = "",
    parent_thread_id: str = "",
    approval_payload_json: Mapping[str, Any] | str | None = None,
) -> tuple[str, str]:
    """Create an approval request and return ``(resume_token, request_id)``."""
    req_id = uuid.uuid4().hex[:12]
    resume_token = uuid.uuid4().hex
    timeout_at = None
    if timeout_minutes > 0:
        timeout_at = (datetime.now() + timedelta(minutes=timeout_minutes)).isoformat()
    conn = _get_conn()
    if isinstance(approval_payload_json, str):
        payload_text = approval_payload_json.strip() or "{}"
    elif approval_payload_json:
        payload_text = json.dumps(approval_payload_json, ensure_ascii=False, sort_keys=True, default=str)
    else:
        payload_text = "{}"
    conn.execute(
        "INSERT INTO approval_requests "
        "(id, run_id, task_id, step_id, resume_token, message, channel, "
        "status, requested_at, timeout_at, agent_run_id, resume_kind, "
        "source_label, source_thread_id, parent_thread_id, approval_payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
        (req_id, run_id, task_id, step_id, resume_token, message,
         channel, datetime.now().isoformat(), timeout_at,
         agent_run_id, resume_kind, source_label, source_thread_id,
         parent_thread_id, payload_text),
    )
    conn.commit()
    conn.close()
    _emit_buddy_approval_event(
        "needed",
        run_id=run_id,
        task_id=task_id,
        step_id=step_id,
        approval_id=req_id,
        resume_token=resume_token,
        label="Approval pending",
        message=message,
    )
    return resume_token, req_id


def _emit_buddy_approval_event(
    status: str,
    *,
    run_id: str = "",
    task_id: str = "",
    step_id: str = "",
    approval_id: str = "",
    resume_token: str = "",
    label: str = "",
    message: str = "",
) -> None:
    try:
        from row_bot.buddy.events import BuddyEventType, emit_buddy_event
        event_type = {
            "needed": BuddyEventType.APPROVAL_NEEDED,
            "approved": BuddyEventType.APPROVAL_APPROVED,
            "denied": BuddyEventType.APPROVAL_DENIED,
            "timed_out": BuddyEventType.APPROVAL_TIMED_OUT,
        }.get(status)
        if event_type is None:
            return
        emit_buddy_event(
            event_type,
            source="tasks",
            payload={
                "run_id": run_id,
                "task_id": task_id,
                "step_id": step_id,
                "approval_id": approval_id,
                "resume_token": resume_token,
                "label": label or message or status.replace("_", " ").title(),
                "message": message,
            },
        )
    except Exception:
        logger.debug("Buddy approval event failed", exc_info=True)


@_schema_retry
def get_pending_approvals(
    *,
    parent_thread_id: str = "",
    agent_run_id: str = "",
    approval_id: str = "",
) -> list[dict]:
    """Return all pending approval requests."""
    conn = _get_conn()
    clauses = ["a.status = 'pending'"]
    params: list[str] = []
    if parent_thread_id:
        clauses.append("a.parent_thread_id = ?")
        params.append(str(parent_thread_id))
    if agent_run_id:
        clauses.append("a.agent_run_id = ?")
        params.append(str(agent_run_id))
    if approval_id:
        clauses.append("a.id = ?")
        params.append(str(approval_id))
    where_sql = " AND ".join(clauses)
    rows = conn.execute(
        "SELECT a.*, t.name AS task_name, t.icon AS task_icon "
        "FROM approval_requests a "
        "LEFT JOIN tasks t ON a.task_id = t.id "
        f"WHERE {where_sql} "
        "ORDER BY a.requested_at DESC",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_approval_for_agent_run(agent_run_id: str) -> dict | None:
    """Return the newest pending approval for a child Agent Run."""

    rows = get_pending_approvals(agent_run_id=str(agent_run_id or ""))
    return rows[0] if rows else None


@_schema_retry
def get_approval_request_statuses(approval_ids: Sequence[str]) -> dict[str, str]:
    """Return authoritative statuses for a bounded set of approval cards."""

    clean_ids = list(
        dict.fromkeys(
            str(approval_id or "").strip()
            for approval_id in approval_ids
            if str(approval_id or "").strip()
        )
    )[:200]
    if not clean_ids:
        return {}
    conn = _get_conn()
    try:
        placeholders = ", ".join("?" for _ in clean_ids)
        rows = conn.execute(
            f"SELECT id, status FROM approval_requests WHERE id IN ({placeholders})",
            clean_ids,
        ).fetchall()
    finally:
        conn.close()
    return {str(row["id"]): str(row["status"] or "") for row in rows}


@_schema_retry
def respond_to_approval(resume_token: str, approved: bool,
                         note: str = "",
                         source: str = "web") -> bool:
    """Approve or deny a pending request. Returns True if found and processed."""
    resume_pipeline = _resume_pipeline
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM approval_requests WHERE resume_token = ? AND status = 'pending'",
        (resume_token,),
    ).fetchone()
    if not row:
        conn.close()
        return False

    r = dict(row)
    # Check if the approval has expired
    timeout_at = r.get("timeout_at")
    if timeout_at and timeout_at < datetime.now().isoformat():
        conn.execute(
            "UPDATE approval_requests SET status = 'timed_out', responded_at = ? "
            "WHERE id = ?",
            (datetime.now().isoformat(), r["id"]),
        )
        conn.commit()
        conn.close()
        logger.info("Approval %s was already expired when user responded", r["id"])
        _emit_buddy_approval_event(
            "timed_out",
            run_id=str(r.get("run_id") or ""),
            task_id=str(r.get("task_id") or ""),
            step_id=str(r.get("step_id") or ""),
            approval_id=str(r.get("id") or ""),
            resume_token=str(r.get("resume_token") or ""),
            label="Approval timed out",
            message=str(r.get("message") or ""),
        )
        return False

    new_status = "approved" if approved else "denied"
    changed = conn.execute(
        "UPDATE approval_requests SET status = ?, responded_at = ?, response_note = ? "
        "WHERE resume_token = ? AND status = 'pending'",
        (new_status, datetime.now().isoformat(), note, resume_token),
    ).rowcount
    if not changed:
        conn.rollback()
        conn.close()
        return False
    conn.commit()

    approval_id = dict(row)["id"]
    conn.close()

    _emit_buddy_approval_event(
        new_status,
        run_id=str(r.get("run_id") or ""),
        task_id=str(r.get("task_id") or ""),
        step_id=str(r.get("step_id") or ""),
        approval_id=approval_id,
        resume_token=resume_token,
        label="Approved" if approved else "Denied",
        message=str(r.get("message") or ""),
    )

    # Update all channel messages (cross-channel resolution)
    _resolve_approval_on_channels(approval_id, new_status, source_channel=source)

    # Resume the pipeline — for graph-interrupted steps, denial resumes
    # the graph with approved=False so the tool returns "cancelled" and
    # the step (and subsequent steps) can still complete.
    if str(r.get("resume_kind") or "") == "agent_run":
        from row_bot.agent_runner import resume_agent_run

        resume_agent_run(
            str(r.get("agent_run_id") or r.get("run_id") or ""),
            resume_token=resume_token,
            approved=approved,
        )
    elif str(r.get("resume_kind") or "") == "parent_orchestration":
        from row_bot.agent_orchestrator import resume_parent_orchestration

        orchestration_id = str(r.get("step_id") or "").removeprefix(
            "orchestration:"
        )
        resume_parent_orchestration(
            orchestration_id,
            resume_token=resume_token,
            approved=approved,
        )
    else:
        resume_pipeline(resume_token, approved=approved)
    return True


def _check_approval_timeouts() -> None:
    """Check for expired approval requests and apply timeout action."""
    conn = _get_conn()
    now = datetime.now().isoformat()
    expired = conn.execute(
        "SELECT * FROM approval_requests "
        "WHERE status = 'pending' AND timeout_at IS NOT NULL AND timeout_at < ?",
        (now,),
    ).fetchall()
    for row in expired:
        r = dict(row)
        conn.execute(
            "UPDATE approval_requests SET status = 'timed_out', responded_at = ? "
            "WHERE id = ?",
            (now, r["id"]),
        )
        # Resume pipeline with denial — for graph-interrupted steps
        # this lets the tool return "cancelled" and the pipeline
        # continues to subsequent steps.  For explicit approval steps
        # this stops the pipeline.
        _resolve_approval_on_channels(r["id"], "timed_out",
                                      source_channel="system")
        _emit_buddy_approval_event(
            "timed_out",
            run_id=str(r.get("run_id") or ""),
            task_id=str(r.get("task_id") or ""),
            step_id=str(r.get("step_id") or ""),
            approval_id=str(r.get("id") or ""),
            resume_token=str(r.get("resume_token") or ""),
            label="Approval timed out",
            message=str(r.get("message") or ""),
        )
        if str(r.get("resume_kind") or "") == "agent_run":
            from row_bot.agent_runner import resume_agent_run

            resume_agent_run(
                str(r.get("agent_run_id") or r.get("run_id") or ""),
                resume_token=str(r.get("resume_token") or ""),
                approved=False,
            )
        elif str(r.get("resume_kind") or "") == "parent_orchestration":
            from row_bot.agent_orchestrator import resume_parent_orchestration

            resume_parent_orchestration(
                str(r.get("step_id") or "").removeprefix("orchestration:"),
                resume_token=str(r.get("resume_token") or ""),
                approved=False,
            )
        else:
            _resume_pipeline(r["resume_token"], approved=False)
        logger.info("Approval request %s timed out for task %s",
                     r["id"], r["task_id"])
    if expired:
        conn.commit()
    conn.close()


def _resume_graph_interrupted(
    state: dict,
    task: dict,
    thread_id: str,
    enabled_tool_names: list[str],
    paused_step_index: int,
    approved: bool = True,
) -> None:
    """Resume a graph-interrupted pipeline step in a background thread.

    Calls ``resume_invoke_agent()`` with ``Command(resume=<approved>)``
    to continue the LangGraph graph from where the interrupt paused it.
    When *approved* is False the tool returns a cancellation message
    and the LLM can still finish the step normally.
    If the result is another interrupt, pauses again with a new approval.
    If the result is text, continues pipeline with remaining steps.
    """
    from row_bot.agent import (
        resume_invoke_agent, TaskStoppedError,
        _approval_mode_var, _background_workflow_var, _persistent_thread_var,
    )

    run_id = state["run_id"]
    task_id = state["task_id"]
    config = state.get("config", {})
    config = {
        **config,
        "configurable": {
            **(config.get("configurable") or {}),
            "runtime_surface": "workflow",
            "runtime_mode": "agent",
            "approval_mode": get_task_approval_mode(task),
        },
    }
    step_outputs = state.get("step_outputs", {})
    steps = task.get("steps") or []
    total = len(steps)
    paused_step = steps[paused_step_index] if paused_step_index < total else {}
    step_id = paused_step.get("id", f"step_{paused_step_index + 1}")
    approval_mode = get_task_approval_mode(task)
    effective_tool_names = enabled_tool_names

    # Clear the "(paused)" suffix from the thread name
    from row_bot.threads import _save_thread_meta, _list_threads as _lt
    if any(t[0] == thread_id for t in _lt()):
        _ts = datetime.now().strftime('%b %d, %I:%M %p')
        _save_thread_meta(thread_id, f"⚡ {task['name']} — {_ts}")

    def _run():
        _background_workflow_var.set(True)
        _approval_mode_var.set(approval_mode)
        _persistent_thread_var.set(bool(task.get("persistent_thread_id")))

        _stop_event = threading.Event()
        try:
            result = resume_invoke_agent(
                effective_tool_names, config, approved=approved,
                stop_event=_stop_event,
            )
        except TaskStoppedError:
            _update_pipeline_status(run_id, "stopped")
            _finish_run(run_id, "stopped",
                        status_message="Stopped during graph resume")
            _emit_buddy_workflow_event(
                "cancelled",
                task_id=task_id,
                thread_id=thread_id,
                label="Workflow stopped",
            )
            return
        except Exception as exc:
            exc_str = str(exc).lower()
            if "checkpoint" in exc_str or "no state" in exc_str or "not found" in exc_str:
                err_msg = "Graph checkpoint was lost — cannot resume (task may need to re-run)"
            else:
                err_msg = f"Graph resume error: {exc}"
            logger.error("Graph resume failed for task '%s': %s",
                         task["name"], exc)
            _update_pipeline_status(run_id, "failed")
            _finish_run(run_id, "failed",
                        status_message=err_msg)
            _emit_buddy_workflow_event(
                "error",
                task_id=task_id,
                thread_id=thread_id,
                label="Workflow error",
                error=err_msg,
            )
            return

        # ── Chained interrupt: agent hit another dangerous tool ─────
        if isinstance(result, dict) and result.get("type") == "terminal":
            terminal_reason = str(result.get("terminal_reason") or "")
            message = str(result.get("message") or "Workflow step stopped incomplete.")
            step_outputs[step_id] = message
            _update_pipeline_status(run_id, "blocked")
            _finish_run(
                run_id,
                "blocked",
                status_message=message,
                terminal_reason=terminal_reason,
            )
            _emit_buddy_workflow_event(
                "error",
                task_id=task_id,
                thread_id=thread_id,
                label="Workflow needs attention",
                error=message,
            )
            return
        if isinstance(result, dict) and result.get("type") == "interrupt":
            interrupts = result.get("interrupts", [])

            # Empty interrupts — auto-continue
            if not interrupts:
                logger.warning(
                    "Task '%s' graph resume: empty interrupt — auto-continuing",
                    task["name"],
                )
                # Treat as success — result is empty, keep going
                pass  # fall through to success path below
            elif approval_mode == "block":
                # Block mode — refuse the chained interrupt
                logger.info(
                    "Task '%s' graph resume: chained interrupt in block mode — refusing",
                    task["name"],
                )
                try:
                    result = resume_invoke_agent(
                        effective_tool_names, config,
                        approved=False, stop_event=_stop_event,
                    )
                except Exception as exc2:
                    logger.error("Block-mode resume denial failed: %s", exc2)
                # Fall through to success path
            elif approval_mode == "allow_all":
                # Allow_all — auto-approve the chained interrupt
                logger.info(
                    "Task '%s' graph resume: chained interrupt in allow_all — auto-approving",
                    task["name"],
                )
                try:
                    result = resume_invoke_agent(
                        effective_tool_names, config,
                        approved=True, stop_event=_stop_event,
                    )
                except Exception as exc2:
                    logger.error("Allow-all auto-resume failed: %s", exc2)
                # Fall through to success path
            else:
                # Approve mode — create a new approval request
                details = []
                for intr in interrupts:
                    tool_name = intr.get("tool", "unknown tool")
                    desc = intr.get("description", "")
                    details.append(desc or f"Tool '{tool_name}' needs approval")
                approval_msg = (
                    f"Step {paused_step_index + 1}/{total}: "
                    + "; ".join(details)
                )
                resume_token, approval_req_id = create_approval_request(
                    run_id=run_id,
                    task_id=task_id,
                    step_id=step_id,
                    message=approval_msg,
                )
                _save_pipeline_state(
                    run_id=run_id,
                    task_id=task_id,
                    thread_id=thread_id,
                    current_step_index=paused_step_index,
                    step_outputs=step_outputs,
                    config=config,
                    resume_token=resume_token,
                    status="paused",
                    graph_interrupted=True,
                )
                logger.info(
                    "Task '%s' paused again at step %d/%d — chained interrupt: %s",
                    task["name"], paused_step_index + 1, total, approval_msg,
                )
                _push_approval_to_channels(
                    task, approval_req_id, resume_token, approval_msg,
                )
                from row_bot.notifications import notify
                notify(
                    title="⏸️ Approval Required",
                    message=f"{task['name']}: {approval_msg}",
                    sound="workflow",
                    icon="⏸️",
                )
                return

        if isinstance(result, dict) and result.get("type") == "terminal":
            terminal_reason = str(result.get("terminal_reason") or "")
            message = str(result.get("message") or "Workflow step stopped incomplete.")
            step_outputs[step_id] = message
            _update_pipeline_status(run_id, "blocked")
            _finish_run(
                run_id,
                "blocked",
                status_message=message,
                terminal_reason=terminal_reason,
            )
            _emit_buddy_workflow_event(
                "error",
                task_id=task_id,
                thread_id=thread_id,
                label="Workflow needs attention",
                error=message,
            )
            return

        # ── Success: graph finished the step ────────────────────────
        # Clear the graph_interrupted flag now that the resume succeeded
        _clear_graph_interrupted(run_id)

        if result:
            step_outputs[step_id] = result if isinstance(result, str) else str(result)

        next_step = paused_step_index + 1
        if next_step >= total:
            # Pipeline complete — deliver to channels
            last_output = step_outputs.get(step_id, "") or ""
            deliver_text = last_output or f"✅ Task '{task['name']}' completed."
            delivery_status, delivery_detail = _deliver_to_channels(
                task, deliver_text,
            )
            final_status = _workflow_final_status_for_delivery(delivery_status)
            _update_pipeline_status(run_id, final_status)
            _finish_run(run_id, final_status,
                        status_message=delivery_detail or "Completed after graph resume")
            _emit_buddy_workflow_event(
                "done",
                task_id=task_id,
                thread_id=thread_id,
                label=task.get("name", "Workflow done"),
            )
            update_task(task_id, last_run=datetime.now().isoformat())
            # Fire completion triggers
            try:
                _fire_completion_triggers(task_id)
            except Exception as exc:
                logger.error("Completion trigger error: %s", exc)
            return

        # More steps remain — continue pipeline
        run_task_background(
            task_id, thread_id, enabled_tool_names,
            start_step=next_step,
            notification=True,
            resume_step_outputs=step_outputs,
            resume_run_id=run_id,
        )

    t = threading.Thread(target=_run, daemon=True,
                         name=f"graph-resume-{task['name']}")
    t.start()


def _resume_pipeline(resume_token: str, approved: bool = True) -> None:
    """Resume a paused pipeline from saved state.

    *approved* is ``False`` when the user denied the approval.  For
    graph-interrupted prompt steps the graph is resumed with
    ``approved=False`` so the tool returns a cancellation message and
    the step (and remaining steps) can still complete.  For explicit
    approval-type steps, denial stops the entire pipeline.
    """
    from row_bot.tools import registry as tool_registry
    from row_bot.threads import _save_thread_meta, _list_threads

    state = _load_pipeline_state(resume_token)
    if not state:
        logger.error("Cannot resume: no pipeline state for token %s", resume_token)
        return

    task = get_task(state["task_id"])
    if not task:
        logger.error("Cannot resume: task %s not found", state["task_id"])
        return

    thread_id = state["thread_id"]
    enabled = [t.name for t in tool_registry.get_enabled_tools()]

    steps = task["steps"]
    paused_step_index = state["current_step_index"]
    paused_step = steps[paused_step_index] if paused_step_index < len(steps) else {}
    paused_step_type = paused_step.get("type", "prompt")

    if state.get("graph_interrupted") == "true":
        _update_pipeline_status(state["run_id"], "running")
        _resume_graph_interrupted(
            state=state,
            task=task,
            thread_id=thread_id,
            enabled_tool_names=enabled,
            paused_step_index=paused_step_index,
            approved=approved,
        )
        return

    # For explicit approval steps, denial stops the pipeline
    # unless an if_denied jump target is configured.
    if paused_step_type == "approval" and not approved:
        denied_target = paused_step.get("if_denied", "")
        if denied_target and denied_target != "end":
            # Jump to the specified step
            resolved = _resolve_step_index(steps, denied_target)
            if resolved is not None:
                _update_pipeline_status(state["run_id"], "running")
                logger.info(
                    "Task '%s' approval denied → jumping to '%s'",
                    task["name"], denied_target,
                )
                step_outputs = state.get("step_outputs", {})
                step_outputs[paused_step.get("id", f"step_{paused_step_index+1}")] = "denied"
                run_task_background(
                    state["task_id"], thread_id, enabled,
                    start_step=resolved,
                    notification=True,
                    resume_step_outputs=step_outputs,
                    resume_run_id=state["run_id"],
                )
                return
        # No target or "end" — stop the pipeline
        _update_pipeline_status(state["run_id"], "stopped")
        _finish_run(state["run_id"], "stopped",
                    status_message="Approval denied by user")
        _emit_buddy_workflow_event(
            "cancelled",
            task_id=state["task_id"],
            thread_id=thread_id,
            label="Workflow denied",
        )
        if any(t[0] == thread_id for t in _list_threads()):
            thread_name = (f"⚡ {task['name']} (denied) — "
                           f"{datetime.now().strftime('%b %d, %I:%M %p')}")
            _save_thread_meta(thread_id, thread_name)
        from row_bot.notifications import notify
        notify(title="❌ Task Denied",
               message=f"{task['name']}: approval denied by user",
               sound="workflow", icon="❌")
        return

    _update_pipeline_status(state["run_id"], "running")

    # For explicit approval steps, resume at the NEXT step
    # or jump to the if_approved target if configured.
    # For graph-interrupted prompt steps (approve mode), use
    # resume_invoke_agent() to continue from the checkpoint (F12).
    if paused_step_type == "approval":
        approved_target = paused_step.get("if_approved", "")
        if approved_target == "end":
            _update_pipeline_status(state["run_id"], "completed")
            _finish_run(state["run_id"], "completed",
                        status_message="Completed (approved → end)")
            _emit_buddy_workflow_event(
                "done",
                task_id=state["task_id"],
                thread_id=thread_id,
                label=task.get("name", "Workflow done"),
            )
            return
        if approved_target:
            resolved = _resolve_step_index(steps, approved_target)
            next_step = resolved if resolved is not None else paused_step_index + 1
        else:
            next_step = paused_step_index + 1
        # Store approval result in step outputs
        step_outputs = state.get("step_outputs", {})
        step_outputs[paused_step.get("id", f"step_{paused_step_index+1}")] = "approved"
        resume_run = state["run_id"]
    else:
        next_step = paused_step_index
        step_outputs = state.get("step_outputs", {})
        resume_run = state["run_id"]

    if next_step >= len(steps):
        # No more steps — just finish
        _update_pipeline_status(state["run_id"], "completed")
        _finish_run(state["run_id"], "completed",
                    status_message="Completed after approval")
        _emit_buddy_workflow_event(
            "done",
            task_id=state["task_id"],
            thread_id=thread_id,
            label=task.get("name", "Workflow done"),
        )
        return

    # Clear the "(paused)" suffix from the thread name
    from row_bot.threads import _save_thread_meta, _list_threads
    if any(t[0] == thread_id for t in _list_threads()):
        _ts = datetime.now().strftime('%b %d, %I:%M %p')
        _save_thread_meta(thread_id, f"⚡ {task['name']} — {_ts}")

    # Use step_outputs if set by approval branch, otherwise from state
    run_task_background(
        state["task_id"], thread_id, enabled,
        start_step=next_step,
        resume_run_id=resume_run,
        notification=True,
        resume_step_outputs=step_outputs,
    )


# ── Approval Timeout Monitor ────────────────────────────────────────────────

_approval_monitor_started = False
_approval_monitor_lock = threading.Lock()


def start_approval_monitor() -> None:
    """Start a background thread that checks for expired approvals."""
    global _approval_monitor_started
    with _approval_monitor_lock:
        if _approval_monitor_started:
            return
        _approval_monitor_started = True

    def _monitor():
        import time
        while True:
            try:
                _check_approval_timeouts()
            except Exception as exc:
                logger.error("Approval monitor error: %s", exc)
            time.sleep(60)  # check every 60 seconds

    t = threading.Thread(target=_monitor, daemon=True, name="approval-monitor")
    t.start()
    logger.info("Approval timeout monitor started")


# ── Subtask Execution ────────────────────────────────────────────────────────

def _run_subtask_sync(
    child_task: dict,
    thread_id: str,
    tool_names: list[str],
    parent_config: dict,
    stop_event: threading.Event,
    parent_output: str = "",
    depth: int = 1,
) -> str | None:
    """Run a subtask synchronously and return its final output.

    Executes inline (not in a new thread) so the parent can wait for the
    result and use it as ``step_outputs``.
    """
    from row_bot.agent import invoke_agent, TaskStoppedError

    token = _subtask_depth_var.set(depth)
    try:
        steps = child_task.get("steps") or _prompts_to_steps(
            child_task.get("prompts") or []
        )
        if not steps:
            return None

        # Apply child task's approval mode
        child_approval = get_task_approval_mode(child_task)
        effective_tools = tool_names

        config = {
            "configurable": {
                "thread_id": thread_id,
                "runtime_surface": "workflow",
                "runtime_mode": "agent",
                "approval_mode": child_approval,
            },
        }
        if child_task.get("model_override"):
            config["configurable"]["model_override"] = child_task["model_override"]

        last_output = parent_output
        step_outputs: dict[str, str] = {}

        i = 0
        while i < len(steps):
            if stop_event.is_set():
                return None

            step = steps[i]
            step_id = step.get("id", f"child_step_{i + 1}")
            step_type = step.get("type", "prompt")

            if step_type == "prompt":
                prompt = step.get("prompt", "")
                prompt = expand_template_vars(
                    prompt, task_id=child_task["id"],
                    prev_output=last_output,
                    step_outputs=step_outputs,
                )
                # Inject parent output
                prompt = prompt.replace("{{parent_output}}", parent_output)

                try:
                    result = invoke_agent(prompt, effective_tools, config,
                                         stop_event=stop_event)
                    if isinstance(result, dict) and result.get("type") == "terminal":
                        logger.warning(
                            "Subtask '%s' step %d stopped incomplete: %s",
                            child_task["name"],
                            i + 1,
                            result.get("terminal_reason") or "terminal",
                        )
                        return None
                    # Subtasks do not support approval flow — if the
                    # agent triggered an interrupt(), handle it inline.
                    if isinstance(result, dict) and result.get("type") == "interrupt":
                        child_approval = get_task_approval_mode(child_task)
                        if child_approval == "allow_all":
                            from row_bot.agent import resume_invoke_agent
                            result = resume_invoke_agent(
                                effective_tools, config,
                                approved=True, stop_event=stop_event,
                            )
                            if isinstance(result, str) and result:
                                last_output = result
                                step_outputs[step_id] = result
                        else:
                            # Block or approve — refuse since subtasks
                            # can't surface approval UI to the user.
                            from row_bot.agent import resume_invoke_agent
                            denied_result = resume_invoke_agent(
                                effective_tools, config,
                                approved=False, stop_event=stop_event,
                            )
                            logger.warning(
                                "Subtask '%s' step %d: interrupt denied "
                                "(subtasks cannot surface approvals)",
                                child_task["name"], i + 1,
                            )
                            # Preserve the LLM's actual response if available
                            if isinstance(denied_result, str) and denied_result:
                                last_output = denied_result
                            else:
                                last_output = (
                                    "⚠️ A tool required approval but subtasks "
                                    "cannot surface approval requests. The "
                                    "tool call was denied."
                                )
                            step_outputs[step_id] = last_output
                    elif result:
                        last_output = result if isinstance(result, str) else str(result)
                        step_outputs[step_id] = last_output
                except TaskStoppedError:
                    return None
                except Exception as exc:
                    logger.error(
                        "Subtask '%s' step %d failed: %s",
                        child_task["name"], i + 1, exc,
                    )
                    on_err = step.get("on_error", "stop")
                    if on_err == "stop":
                        return None

            elif step_type == "condition":
                cond_expr = step.get("condition", "true")
                cond_ctx = {
                    "prev_output": last_output,
                    "step_outputs": step_outputs,
                    "task_id": child_task["id"],
                }
                result = evaluate_condition(cond_expr, cond_ctx)
                step_outputs[step_id] = str(result)
                target = step.get("if_true") if result else step.get("if_false")
                logger.info(
                    "Subtask '%s' condition step %d: '%s' → %s → jump to %s",
                    child_task["name"], i + 1, cond_expr, result, target,
                )
                if target:
                    resolved = _resolve_step_index(steps, target)
                    if resolved is None:
                        break  # target is "end"
                    if resolved == i:
                        logger.error(
                            "Subtask '%s' condition step %d would jump to itself — stopping",
                            child_task["name"], i + 1,
                        )
                        break
                    i = resolved
                    continue  # jump — skip the i += 1 below

            elif step_type == "notify":
                msg = step.get("message", "")
                msg = expand_template_vars(
                    msg, task_id=child_task["id"],
                    prev_output=last_output,
                    step_outputs=step_outputs,
                )
                notify_channel = step.get("channel", "desktop")
                if notify_channel == "desktop":
                    from row_bot.notifications import notify as _notify
                    _notify(
                        title=f"📋 {child_task['name']}",
                        message=msg,
                        sound="workflow",
                        icon="📋",
                    )
                else:
                    try:
                        _deliver_to_channel(
                            {**child_task, "delivery_channel": notify_channel},
                            msg,
                        )
                    except Exception as exc:
                        logger.error(
                            "Subtask '%s' step %d notify failed: %s",
                            child_task["name"], i + 1, exc,
                        )
                step_outputs[step_id] = msg

            else:
                logger.warning(
                    "Subtask '%s' step %d: unsupported step type '%s' — skipping",
                    child_task["name"], i + 1, step_type,
                )

            # ── Optional "next" override on any step ─────────────
            next_target = step.get("next")
            if next_target:
                resolved = _resolve_step_index(steps, next_target)
                if resolved is None:
                    break  # "end"
                i = resolved
                continue  # skip the default i += 1

            i += 1

        return last_output
    finally:
        _subtask_depth_var.reset(token)


def detect_circular_subtasks(task_id: str, steps: list[dict]) -> list[str]:
    """Check for circular subtask references. Returns cycle path if found."""
    def _dfs(tid: str, visited: set[str], path: list[str]) -> list[str] | None:
        if tid in visited:
            return path + [tid]
        visited.add(tid)
        t = get_task(tid)
        if not t:
            return None
        child_steps = t.get("steps") or []
        # If checking the task being saved, use the provided steps
        if tid == task_id:
            child_steps = steps
        for s in child_steps:
            if s.get("type") == "subtask":
                child_id = s.get("task_id", "")
                result = _dfs(child_id, visited.copy(), path + [tid])
                if result:
                    return result
        return None

    for s in steps:
        if s.get("type") == "subtask":
            cycle = _dfs(s.get("task_id", ""), set(), [task_id])
            if cycle:
                return cycle
    return []


# ── Condition Evaluator ──────────────────────────────────────────────────────

def evaluate_condition(expr: str, context: dict) -> bool:
    """Evaluate a condition expression against a context dict.

    *context* typically contains::

        {
            "prev_output": str,          # output of the previous step
            "step_outputs": dict,        # all step outputs so far
            "task_id": str,
        }

    Supported operators::

        contains:<text>         — case-insensitive substring check
        not_contains:<text>     — negation of contains
        equals:<text>           — exact string match
        matches:<regex>         — Python re.search
        gt:<n>  lt:<n>  gte:<n>  lte:<n>  — first number comparison
        length_gt:<n>  length_lt:<n>      — character count
        empty / not_empty
        true / false                      — forced branching
        json:<path>:<op>:<value>          — nested JSON field check
        llm:<prompt>                      — LLM evaluation (yes/no)
        and:[cond1,cond2,...]             — all must be true
        or:[cond1,cond2,...]              — any must be true
    """
    expr = expr.strip()
    prev = context.get("prev_output", "")

    # ── Boolean literals ─────────────────────────────────────────────
    if expr == "true":
        return True
    if expr == "false":
        return False

    # ── Empty / not_empty ────────────────────────────────────────────
    if expr == "empty":
        return not prev.strip()
    if expr == "not_empty":
        return bool(prev.strip())

    # ── contains / not_contains ──────────────────────────────────────
    if expr.startswith("contains:"):
        needle = expr[len("contains:"):]
        return needle.lower() in prev.lower()
    if expr.startswith("not_contains:"):
        needle = expr[len("not_contains:"):]
        return needle.lower() not in prev.lower()

    # ── equals ───────────────────────────────────────────────────────
    if expr.startswith("equals:"):
        return prev == expr[len("equals:"):]

    # ── matches (regex) ──────────────────────────────────────────────
    if expr.startswith("matches:"):
        import re as _re
        pattern = expr[len("matches:"):]
        try:
            return bool(_re.search(pattern, prev))
        except _re.error:
            logger.warning("Invalid regex in condition: %s", pattern)
            return False

    # ── Numeric comparisons ──────────────────────────────────────────
    _numeric_ops = {"gt:": ">", "lt:": "<", "gte:": ">=", "lte:": "<="}
    for prefix, _op in _numeric_ops.items():
        if expr.startswith(prefix):
            import re as _re
            threshold = expr[len(prefix):]
            try:
                threshold_val = float(threshold)
            except ValueError:
                logger.warning("Invalid number in condition: %s", threshold)
                return False
            # Extract first number from prev_output
            match = _re.search(r"-?\d+\.?\d*", prev)
            if not match:
                return False
            actual_val = float(match.group())
            if _op == ">":
                return actual_val > threshold_val
            if _op == "<":
                return actual_val < threshold_val
            if _op == ">=":
                return actual_val >= threshold_val
            if _op == "<=":
                return actual_val <= threshold_val

    # ── Length comparisons ───────────────────────────────────────────
    if expr.startswith("length_gt:"):
        try:
            return len(prev) > int(expr[len("length_gt:"):])
        except ValueError:
            return False
    if expr.startswith("length_lt:"):
        try:
            return len(prev) < int(expr[len("length_lt:"):])
        except ValueError:
            return False

    # ── JSON field check ─────────────────────────────────────────────
    if expr.startswith("json:"):
        return _eval_json_condition(expr[len("json:"):], prev, context)

    # ── LLM evaluation ───────────────────────────────────────────────
    if expr.startswith("llm:"):
        return _eval_llm_condition(expr[len("llm:"):], context)

    # ── Compound: and / or ───────────────────────────────────────────
    if expr.startswith("and:[") and expr.endswith("]"):
        sub_exprs = _split_compound(expr[len("and:["):-1])
        return all(evaluate_condition(s, context) for s in sub_exprs)
    if expr.startswith("or:[") and expr.endswith("]"):
        sub_exprs = _split_compound(expr[len("or:["):-1])
        return any(evaluate_condition(s, context) for s in sub_exprs)

    logger.warning("Unknown condition expression: %s", expr)
    return False


def _split_compound(inner: str) -> list[str]:
    """Split comma-separated condition expressions, respecting nested brackets."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _eval_json_condition(expr: str, prev_output: str, context: dict | None = None) -> bool:
    """Evaluate json:<path>:<op>:<value> against prev_output parsed as JSON."""
    parts = expr.split(":", 2)
    if len(parts) < 2:
        logger.warning("Invalid json condition: %s", expr)
        return False
    json_path = parts[0]
    op_and_val = parts[1] if len(parts) == 2 else f"{parts[1]}:{parts[2]}"

    try:
        data = json.loads(prev_output)
    except (json.JSONDecodeError, TypeError):
        return False

    # Navigate JSON path (dot-separated)
    for key in json_path.split("."):
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list):
            try:
                data = data[int(key)]
            except (ValueError, IndexError):
                return False
        else:
            return False
        if data is None:
            return False

    # Evaluate the sub-condition against the extracted value,
    # preserving step_outputs and task_id from the parent context.
    sub_context = {
        "prev_output": str(data),
        "step_outputs": (context or {}).get("step_outputs", {}),
        "task_id": (context or {}).get("task_id", ""),
    }
    return evaluate_condition(op_and_val, sub_context)


def _eval_llm_condition(prompt: str, context: dict) -> bool:
    """Use the LLM to evaluate a condition. Expects yes/no response."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from row_bot.agent import _chat_only_llm
        from row_bot.models import get_current_model

        prev = context.get("prev_output", "")
        step_outputs = context.get("step_outputs", {})

        # Build context section with all available step outputs
        context_parts = []
        if prev:
            context_parts.append(f"Previous step output:\n{prev}")
        if step_outputs:
            context_parts.append("All step outputs:")
            for sid, sout in step_outputs.items():
                context_parts.append(f"  [{sid}]: {sout}")

        context_text = "\n".join(context_parts) if context_parts else "(no outputs yet)"

        # Guard against excessively large context blowing past token limits
        _MAX_COND_CONTEXT = 32000
        if len(context_text) > _MAX_COND_CONTEXT:
            context_text = context_text[:_MAX_COND_CONTEXT] + "\n[... truncated ...]"  # noqa: E501

        full_prompt = (
            f"Answer ONLY 'yes' or 'no' to the following question.\n\n"
            f"Context:\n{context_text}\n\n"
            f"Question: {prompt}"
        )
        # This is a bounded classification call, not a full agent loop.
        result = _chat_only_llm(get_current_model()).invoke(
            [
                SystemMessage(content="Answer only yes or no. Do not call tools."),
                HumanMessage(content=full_prompt),
            ]
        )
        content = getattr(result, "content", result)
        if content:
            answer = str(content).strip().lower()
            return answer.startswith("yes")
    except Exception as exc:
        logger.error("LLM condition evaluation failed: %s", exc)
    return False


def _resolve_step_index(steps: list[dict], target: str) -> int | None:
    """Resolve a step ID or 'end' to an index. Returns None for 'end'."""
    if target == "end":
        return None  # signals end of pipeline
    for i, s in enumerate(steps):
        if s.get("id") == target:
            return i
    # Try numeric index as fallback
    try:
        idx = int(target)
        if 0 <= idx < len(steps):
            return idx
    except ValueError:
        pass
    logger.warning("Cannot resolve step target '%s' — treating as end", target)
    return None


def assign_step_ids(steps: list[dict]) -> None:
    """Assign {type}_{counter} IDs to steps and update cross-references.

    Mutates the list in-place.  Safe to call repeatedly — existing IDs
    are remapped when they change.
    """
    if not steps:
        return
    type_counters: dict[str, int] = {}
    old_to_new: dict[str, str] = {}
    for s in steps:
        stype = s.get("type", "prompt")
        type_counters[stype] = type_counters.get(stype, 0) + 1
        new_id = f"{stype}_{type_counters[stype]}"
        old_id = s.get("id", "")
        if old_id and old_id != new_id:
            old_to_new[old_id] = new_id
        s["id"] = new_id
    # Remap cross-references (condition branches, approval branches, next)
    if old_to_new:
        for s in steps:
            for field in ("if_true", "if_false", "if_approved", "if_denied", "next"):
                ref = s.get(field)
                if ref and ref in old_to_new:
                    s[field] = old_to_new[ref]


# ── Mermaid diagram generation ───────────────────────────────────────────────

_STEP_ICONS = {
    "prompt": "💬", "condition": "🔀", "approval": "✋",
    "subtask": "🔁", "notify": "📢",
}


_STEP_ICONS["delegate_agent"] = "Agent"
_STEP_ICONS["wait_for_agents"] = "Wait"


def generate_pipeline_mermaid(steps: list[dict]) -> str:
    """Generate a Mermaid flowchart string from pipeline steps."""
    if not steps:
        return ""
    lines = ["graph TD"]
    # Nodes
    for j, s in enumerate(steps):
        sid = s.get("id", f"step_{j+1}")
        stype = s.get("type", "prompt")
        icon = _STEP_ICONS.get(stype, "❓")
        if stype == "prompt":
            txt = (s.get("prompt") or "")[:25]
            label = f"{icon} {txt}" if txt else f"{icon} Prompt"
        elif stype == "condition":
            txt = (s.get("condition") or "")[:25]
            label = f"{icon} {txt}" if txt else f"{icon} Condition"
        elif stype == "approval":
            label = f"{icon} Approval"
        elif stype == "subtask":
            label = f"{icon} Run Workflow"
        elif stype == "delegate_agent":
            txt = (s.get("objective") or s.get("prompt") or "")[:25]
            label = f"{icon} {txt}" if txt else f"{icon} Child Agent"
        elif stype == "wait_for_agents":
            label = "Wait for Agents"
        elif stype == "notify":
            ch = s.get("channel", "")
            label = f"{icon} Notify ({ch})" if ch else f"{icon} Notify"
        else:
            label = f"{icon} {sid}"
        safe = (label.replace('"', "'")
                .replace("<", "&lt;").replace(">", "&gt;")
                .replace("{", "(").replace("}", ")")
                .replace("[", "(").replace("]", ")"))
        if stype in ("condition", "approval"):
            lines.append(f'    {sid}{{{{"{safe}"}}}}')
        else:
            lines.append(f'    {sid}["{safe}"]')
    # Edges
    for j, s in enumerate(steps):
        sid = s.get("id", f"step_{j+1}")
        stype = s.get("type", "prompt")
        if stype == "condition":
            if_true = s.get("if_true", "")
            if_false = s.get("if_false", "")
            t_target = if_true if if_true else (
                steps[j+1].get("id") if j+1 < len(steps) else None
            )
            f_target = if_false if if_false else (
                steps[j+1].get("id") if j+1 < len(steps) else None
            )
            if t_target and t_target != "end":
                lines.append(f'    {sid} -->|"Yes"| {t_target}')
            elif t_target == "end":
                lines.append(f'    {sid} -->|"Yes"| END_NODE["🛑 End"]')
            if f_target and f_target != "end" and f_target != t_target:
                lines.append(f'    {sid} -->|"No"| {f_target}')
            elif f_target == "end":
                lines.append(f'    {sid} -->|"No"| END_NODE["🛑 End"]')
        elif stype == "approval":
            if_appr = s.get("if_approved", "")
            if_deny = s.get("if_denied", "")
            a_target = if_appr if if_appr else (
                steps[j+1].get("id") if j+1 < len(steps) else None
            )
            d_target = if_deny if if_deny else "end"
            if a_target and a_target != "end":
                lines.append(f'    {sid} -->|"Approved"| {a_target}')
            elif a_target == "end":
                lines.append(f'    {sid} -->|"Approved"| END_NODE["🛑 End"]')
            if d_target != "end":
                if d_target and d_target != a_target:
                    lines.append(f'    {sid} -->|"Denied"| {d_target}')
            else:
                lines.append(f'    {sid} -->|"Denied"| END_NODE["🛑 End"]')
        else:
            # Check for explicit "next" override
            next_target = s.get("next")
            if next_target:
                if next_target == "end":
                    lines.append(f'    {sid} --> END_NODE["🛑 End"]')
                else:
                    lines.append(f'    {sid} --> {next_target}')
            elif j + 1 < len(steps):
                next_sid = steps[j+1].get("id")
                lines.append(f'    {sid} --> {next_sid}')
    return "\n".join(lines)


# ── Default Templates ────────────────────────────────────────────────────────

_LEGACY_DEFAULT_TASKS = [
    {
        "name": "Morning Briefing",
        "description": "News, weather, and today's calendar — delivered every morning",
        "icon": "🌅",
        "prompts": [
            "Give me a brief summary of the top 5 news stories today.",
            "What's the weather forecast for today and tomorrow?",
            "What events do I have on my calendar for {{date}}?",
            "Now combine everything above into a single morning briefing. "
            "Start with the weather, then calendar, then news headlines.",
        ],
        "schedule": "daily:08:00",
    },
    {
        "name": "Research Digest",
        "description": "Weekly AI research roundup with sources",
        "icon": "🔬",
        "prompts": [
            "Search the web for the latest developments in artificial intelligence this week. "
            "Find at least 5 notable stories, papers, or breakthroughs.",
            "Now summarize your findings into a well-structured weekly digest with bullet points "
            "and source citations for each item. Group by category (models, applications, policy).",
        ],
        "schedule": "weekly:fri:17:00",
    },
    {
        "name": "Inbox Zero",
        "description": "Check and triage unread emails",
        "icon": "📧",
        "prompts": [
            "Check my Gmail inbox for any unread or recent emails from today.",
            "Summarize each email in 1-2 sentences, grouped by priority "
            "(action required vs. informational). List the sender and subject for each.",
        ],
        "schedule": "daily:09:00",
    },
    {
        "name": "Weekly Review",
        "description": "Recap of the past week's events and priorities",
        "icon": "📋",
        "prompts": [
            "What events did I have on my calendar this past week (last 7 days)?",
            "Based on these events, write a short weekly review summarizing what I was busy "
            "with this week. Highlight any patterns and suggest priorities for next week.",
        ],
        "schedule": "weekly:sun:18:00",
    },
    {
        "name": "Stand-Up Reminder",
        "description": "Gentle reminder to stand up and stretch",
        "icon": "🧘",
        "notify_only": True,
        "notify_label": "Time to stand up and stretch! 🧘",
        "prompts": [],
        "schedule": "interval:2",
    },
]


_DEFAULT_TASKS = [
    {
        "name": "Daily Operating Brief",
        "description": "Manual daily planning brief across calendar, weather, news, and priorities",
        "icon": "🌅",
        "complexity": "simple",
        "steps": [
            {
                "type": "prompt",
                "prompt": "Collect today's planning context. Check calendar, weather, and current news if those tools/accounts are configured. "
                "Also consider any known priorities from memory. If something is not configured, mark it as unavailable and continue.",
            },
            {
                "type": "prompt",
                "prompt": "Create a concise daily operating brief from the collected context. Include: schedule, weather, important external context, "
                "top 3 priorities, risks, and a recommended first action.\n\nCollected context:\n{{prev_output}}",
            },
        ],
        "schedule": None,
        "enabled": False,
    },
    {
        "name": "Document Decision Brief",
        "description": "Manual document review for a chosen topic, decision, or question",
        "icon": "📄",
        "complexity": "simple",
        "steps": [
            {
                "type": "prompt",
                "prompt": "Search uploaded documents and the knowledge base for material related to <topic-or-decision>. "
                "Extract the most relevant facts, quotes or source names, open questions, and contradictions. "
                "Before enabling this workflow, replace <topic-or-decision> with the decision or question to investigate.",
            },
            {
                "type": "prompt",
                "prompt": "Turn the extracted material into a decision brief. Include: answer/recommendation, supporting evidence, risks, "
                "open questions, and next actions.\n\nExtracted material:\n{{prev_output}}",
            },
        ],
        "schedule": None,
        "enabled": False,
    },
    {
        "name": "Launch Content Pack",
        "description": "Manual content pack for a product, project, or announcement",
        "icon": "✍️",
        "complexity": "simple",
        "steps": [
            {
                "type": "prompt",
                "prompt": "Create a positioning brief for <product-or-project> aimed at <audience>. "
                "Before enabling this workflow, replace the placeholders with the product/project and audience. "
                "Include core value proposition, proof points, objections, and tone guidance.",
            },
            {
                "type": "prompt",
                "prompt": "Using the positioning brief, draft a reusable launch content pack: short announcement, email draft, social post, "
                "landing page hero copy, and 5 FAQ bullets.\n\nPositioning brief:\n{{prev_output}}",
            },
        ],
        "schedule": None,
        "enabled": False,
    },
    {
        "name": "Research Pipeline With Review",
        "description": "Advanced manual research pipeline with source extraction and approval",
        "icon": "🔬",
        "complexity": "advanced",
        "schedule": None,
        "enabled": False,
        "steps": [
            {
                "type": "prompt",
                "prompt": "Research <topic> for <audience>. Before enabling this workflow, replace <topic> and <audience>. "
                "Find credible, recent sources and capture the strongest facts, disagreements, and source links.",
            },
            {
                "type": "prompt",
                "prompt": "Turn the research into a structured brief: executive summary, key evidence, "
                "risks/unknowns, and recommended next steps. Include citations or source names where available.\n\n"
                "Prior research:\n{{prev_output}}",
            },
            {
                "type": "approval",
                "message": "Review the research brief before Row-Bot prepares the final shareable report.",
                "timeout_minutes": 120,
            },
            {
                "type": "prompt",
                "prompt": "Prepare the final shareable research report based on the approved brief. "
                "Keep it concise, practical, and source-aware.\n\nApproved brief:\n{{prev_output}}",
            },
        ],
    },
    {
        "name": "Opportunity Monitor",
        "description": "Advanced manual scan with relevance gate and notification step",
        "icon": "📣",
        "complexity": "advanced",
        "schedule": None,
        "enabled": False,
        "steps": [
            {
                "type": "prompt",
                "prompt": "Scan for opportunities related to <market-or-customer-segment>. Before enabling this workflow, replace the placeholder. "
                "Look for leads, partnership openings, product ideas, grants, hiring signals, events, or customer pain points. "
                "Return only opportunities that look actionable. If there are no actionable opportunities, say exactly: NO_ACTIONABLE_OPPORTUNITIES.",
            },
            {
                "type": "condition",
                "condition": "not_contains:NO_ACTIONABLE_OPPORTUNITIES",
                "if_true": "",
                "if_false": "end",
            },
            {
                "type": "prompt",
                "prompt": "Act as a specialist analyst and score the opportunities from the scan. For each one, "
                "estimate relevance, urgency, effort, next action, and why it matters.\n\n"
                "Scan output:\n{{prev_output}}",
            },
            {
                "type": "notify",
                "channel": "desktop",
                "message": "Opportunity Monitor found actionable items. Open the workflow run to review the ranked opportunities.",
            },
        ],
    },
]


def add_default_workflow_templates() -> int:
    """Add any missing starter workflow templates as disabled manual workflows."""
    existing_names = {str(t.get("name") or "") for t in list_tasks()}
    created = 0
    for t in _DEFAULT_TASKS:
        if t["name"] in existing_names:
            continue
        create_task(
            name=t["name"],
            prompts=t.get("prompts", []),
            description=t.get("description", ""),
            icon=t.get("icon", "⚡"),
            schedule=t.get("schedule"),
            notify_only=t.get("notify_only", False),
            notify_label=t.get("notify_label", ""),
            steps=t.get("steps"),
            enabled=t.get("enabled", False),
            channels=t.get("channels"),
            agent_profile_id=DEFAULT_WORKFLOW_AGENT_PROFILE_ID,
            apply_default_skills=False,
        )
        created += 1
    return created


@_schema_retry
def seed_default_tasks() -> None:
    """Insert default task templates on first-ever run only.

    Uses a marker file so that if the user deletes all tasks the defaults
    do NOT reappear on the next restart.
    """
    _MARKER = os.path.join(_DATA_DIR, ".tasks_seeded")
    if os.path.exists(_MARKER):
        return  # Already seeded in a prior run — user may have deleted them
    existing = list_tasks()
    if existing:
        # DB has tasks (e.g. migrated from workflows) — mark as seeded
        open(_MARKER, "w").close()
        return
    for t in _DEFAULT_TASKS:
        create_task(
            name=t["name"],
            prompts=t.get("prompts", []),
            description=t.get("description", ""),
            icon=t.get("icon", "⚡"),
            schedule=t.get("schedule"),
            notify_only=t.get("notify_only", False),
            notify_label=t.get("notify_label", ""),
            steps=t.get("steps"),
            enabled=t.get("enabled", False),
            channels=t.get("channels"),
            agent_profile_id=DEFAULT_WORKFLOW_AGENT_PROFILE_ID,
            apply_default_skills=False,
        )
    open(_MARKER, "w").close()
    logger.info("Seeded %d default tasks", len(_DEFAULT_TASKS))


# Backward-compat aliases for legacy transition
seed_default_workflows = seed_default_tasks
list_workflows = list_tasks


def create_workflow(
    name: str,
    prompts: list[str],
    description: str = "",
    icon: str = "\u26a1",
    schedule: str | None = None,
) -> str:
    return create_task(
        name=name,
        prompts=prompts,
        description=description,
        icon=icon,
        schedule=schedule,
        agent_profile_id=DEFAULT_WORKFLOW_AGENT_PROFILE_ID,
        apply_default_skills=False,
    )


update_workflow = update_task
delete_workflow = delete_task
duplicate_workflow = duplicate_task
run_workflow_background = run_task_background
