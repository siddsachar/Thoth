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

Storage: SQLite at ``~/.thoth/tasks.db``.
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

logger = logging.getLogger(__name__)

# ── Subtask Depth Tracking ───────────────────────────────────────────────────
_subtask_depth_var: ContextVar[int] = ContextVar("subtask_depth", default=0)
_MAX_SUBTASK_DEPTH = 2

# ── Persistence ──────────────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = str(_DATA_DIR / "tasks.db")
_OLD_WF_DB = str(_DATA_DIR / "workflows.db")
_TASK_CONFIG_PATH = str(_DATA_DIR / "task_config.json")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    """Create the tasks and task_runs tables if they don't exist."""
    conn = _get_conn()
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
            channels            TEXT                     -- JSON list of channel names (null = workflow default)
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
            response_note   TEXT
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

    conn = _get_conn()
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
        for row in rows:
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


_init_db()
_migrate_from_workflows()


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
    enabled: bool = True,
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
    # If steps provided, also sync prompts for backward compat
    if steps:
        assign_step_ids(steps)
        prompts = _steps_to_prompts(steps) or prompts
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tasks "
        "(id, name, description, icon, prompts, schedule, at, notify_only, "
        "notify_label, delivery_channel, delivery_target, model_override, "
        "persistent_thread_id, delete_after_run, created_at, enabled, skills_override, "
        "steps, safety_mode, concurrency_group, trigger, tools_override, channels) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, name, description, icon, json.dumps(prompts),
            schedule, at, int(notify_only), notify_label,
            delivery_channel, delivery_target, model_override,
            persistent_thread_id, int(delete_after_run), now, int(enabled),
            json.dumps(skills_override) if skills_override else None,
            json.dumps(steps) if steps else "[]",
            safety_mode,
            concurrency_group,
            json.dumps(trigger) if trigger else None,
            json.dumps(tools_override) if tools_override else None,
            json.dumps(channels) if channels is not None else None,
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


def get_task(task_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_dict(row)


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
        "tools_override", "channels",
    }

    # ── Validate delivery if either field is being changed ───────────
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
        if key == "steps" and isinstance(value, list):
            assign_step_ids(value)
        if key in ("prompts", "allowed_commands", "allowed_recipients",
                   "skills_override", "steps", "trigger",
                   "tools_override", "channels"):
            value = json.dumps(value)
        if key in ("notify_only", "delete_after_run"):
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
            from threads import _delete_thread, purge_external_state
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
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["prompts"] = json.loads(d["prompts"])
    d["notify_only"] = bool(d.get("notify_only", 0))
    d["delete_after_run"] = bool(d.get("delete_after_run", 0))
    d["enabled"] = bool(d.get("enabled", 1))
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
    return run_id


def _update_run_progress(run_id: str, steps_done: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE task_runs SET steps_done = ? WHERE id = ?",
        (steps_done, run_id),
    )
    conn.commit()
    conn.close()


def _finish_run(run_id: str, status: str = "completed",
                status_message: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE task_runs SET status = ?, status_message = ?, finished_at = ? "
        "WHERE id = ?",
        (status, status_message, datetime.now().isoformat(), run_id),
    )
    # Clean up pipeline_state for terminal statuses (no longer needed)
    if status in ("completed", "completed_delivery_failed", "failed", "stopped"):
        conn.execute("DELETE FROM pipeline_state WHERE run_id = ?", (run_id,))
    conn.commit()
    conn.close()


def _emit_buddy_workflow_event(
    status: str,
    *,
    task_id: str = "",
    thread_id: str = "",
    label: str = "",
    error: str = "",
) -> None:
    try:
        from buddy.events import BuddyEventType, emit_buddy_event
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
    from tools import registry as tool_registry

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


def get_run_history(task_id: str, limit: int = 5) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
        from channels import registry as _ch_reg
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
    from channels import registry as _ch_reg
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
        from channels import registry as _ch_reg
        prefix = f"📋 {task['name']}\n\n"
        ch = _ch_reg.get(channel)
        if ch is None:
            raise RuntimeError(f"Unknown channel: {channel}")
        if not ch.is_running():
            raise RuntimeError(f"{ch.display_name} is not running")

        # Resolve target (Telegram uses configured user ID)
        if channel == "telegram":
            from channels.telegram import _get_allowed_user_id
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
                from channels.telegram import _get_allowed_user_id
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
            from channels import registry as _ch_reg
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
        from buddy.events import BuddyEventType, emit_buddy_event
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
        from notifications import notify
        notify(
            title="⏰ Thoth Reminder",
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
        final_status = ("completed_delivery_failed"
                        if delivery_status == "delivery_failed"
                        else "completed")
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
        from agent import invoke_agent, TaskStoppedError
        from threads import _save_thread_meta, _list_threads

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
        step_outputs: dict[str, str] = resume_step_outputs.copy() if resume_step_outputs else {}
        # If resuming, seed last_response from the last step output
        if step_outputs:
            last_response = list(step_outputs.values())[-1]

        # Determine effective safety mode (block/approve/allow_all)
        safety_mode = get_task_safety_mode(task)
        effective_tool_names = enabled_tool_names

        # Skills override — set on thread so the pre-model hook picks it up
        if task.get("skills_override") is not None:
            from threads import set_thread_skills_override
            set_thread_skills_override(thread_id, task["skills_override"])

        # Apply tools_override (explicit selection from UI)
        if task.get("tools_override"):
            override_set = set(task["tools_override"])
            effective_tool_names = [
                t for t in effective_tool_names if t in override_set
            ]
            logger.info(
                "Task '%s' using tools_override — %d tool(s): %s",
                task["name"], len(effective_tool_names), effective_tool_names,
            )

        try:
            from agent import _background_workflow_var, _safety_mode_var, _persistent_thread_var
            _background_workflow_var.set(True)
            _safety_mode_var.set(safety_mode)
            _persistent_thread_var.set(bool(task.get("persistent_thread_id")))

            from agent import RECURSION_LIMIT_TASK
            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": RECURSION_LIMIT_TASK,
            }

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
                from threads import _set_thread_model_override
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
                        from buddy.events import BuddyEventType, emit_buddy_event
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
                            result = invoke_agent(prompt, effective_tool_names, config,
                                                 stop_event=_stop_event)
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
                                    from agent import resume_invoke_agent
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
                                if safety_mode == "block":
                                    logger.info(
                                        "Task '%s' step %d: interrupt in block mode — refusing",
                                        task["name"], step_index + 1,
                                    )
                                    from agent import resume_invoke_agent
                                    result = resume_invoke_agent(
                                        effective_tool_names, config,
                                        approved=False, stop_event=_stop_event,
                                    )
                                    if isinstance(result, str) and result:
                                        last_response = result
                                        step_outputs[step_id] = result
                                    step_succeeded = True
                                    break

                                if safety_mode == "allow_all":
                                    logger.info(
                                        "Task '%s' step %d: interrupt in allow_all mode — auto-approving",
                                        task["name"], step_index + 1,
                                    )
                                    from agent import resume_invoke_agent
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
                                    from notifications import notify
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
                            # Model-fallback retry (existing logic)
                            if (config["configurable"].get("model_override")
                                    and ("model failed to load" in err_str
                                         or "status code: 500" in err_str)):
                                override = config["configurable"].pop("model_override")
                                logger.warning(
                                    "Task %s step %d: override model '%s' failed — "
                                    "retrying with default model. (%s)",
                                    task["name"], step_index + 1, override, exc,
                                )
                                try:
                                    result = invoke_agent(prompt, effective_tool_names, config,
                                                         stop_event=_stop_event)
                                    # Interrupt detection on model-fallback retry
                                    if isinstance(result, dict) and result.get("type") == "interrupt":
                                        if _stop_event.is_set():
                                            stopped = True
                                            break

                                        interrupts = result.get("interrupts", [])
                                        if not interrupts:
                                            logger.warning(
                                                "Task '%s' step %d: empty interrupt (fallback) — auto-continuing",
                                                task["name"], step_index + 1,
                                            )
                                            from agent import resume_invoke_agent
                                            result = resume_invoke_agent(
                                                effective_tool_names, config,
                                                approved=True, stop_event=_stop_event,
                                            )
                                            if isinstance(result, str) and result:
                                                last_response = result
                                                step_outputs[step_id] = result
                                            step_succeeded = True
                                            break

                                        if safety_mode == "block":
                                            logger.info(
                                                "Task '%s' step %d: interrupt (fallback) in block mode — refusing",
                                                task["name"], step_index + 1,
                                            )
                                            from agent import resume_invoke_agent
                                            result = resume_invoke_agent(
                                                effective_tool_names, config,
                                                approved=False, stop_event=_stop_event,
                                            )
                                            if isinstance(result, str) and result:
                                                last_response = result
                                                step_outputs[step_id] = result
                                            step_succeeded = True
                                            break

                                        if safety_mode == "allow_all":
                                            logger.info(
                                                "Task '%s' step %d: interrupt (fallback) in allow_all — auto-approving",
                                                task["name"], step_index + 1,
                                            )
                                            from agent import resume_invoke_agent
                                            result = resume_invoke_agent(
                                                effective_tool_names, config,
                                                approved=True, stop_event=_stop_event,
                                            )
                                            if isinstance(result, str) and result:
                                                last_response = result
                                                step_outputs[step_id] = result
                                            step_succeeded = True
                                            break

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
                                            "Task '%s' paused at step %d/%d — tool interrupt (fallback): %s",
                                            task["name"], step_index + 1, total, approval_msg,
                                        )
                                        _push_approval_to_channels(
                                            task, approval_req_id, resume_token, approval_msg,
                                        )
                                        if notification:
                                            from notifications import notify
                                            notify(
                                                title="⏸️ Approval Required",
                                                message=f"{task['name']}: {approval_msg}",
                                                sound="workflow",
                                                icon="⏸️",
                                            )
                                        break
                                    if result:
                                        last_response = result
                                        step_outputs[step_id] = result
                                    step_succeeded = True
                                    break
                                except TaskStoppedError:
                                    stopped = True
                                    logger.info("Task '%s' stopped during step %d/%d (retry)",
                                                task["name"], step_index + 1, total)
                                    break
                                except Exception as retry_exc:
                                    logger.error(
                                        "Task %s step %d model fallback failed: %s",
                                        task["name"], step_index + 1, retry_exc,
                                    )
                            else:
                                logger.error(
                                    "Task %s step %d attempt %d failed: %s",
                                    task["name"], step_index + 1, step_attempt, exc,
                                )
                            try:
                                from agent import repair_orphaned_tool_calls
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
                        from notifications import notify
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
                elif step_type == "notify":
                    notify_msg = step.get("message", "")
                    notify_msg = expand_template_vars(
                        notify_msg, task_id=task_id,
                        prev_output=last_response,
                        step_outputs=step_outputs,
                    )
                    notify_channel = step.get("channel", "desktop")
                    if notify_channel == "desktop":
                        from notifications import notify as _notify
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
                    from agent import repair_orphaned_tool_calls
                    repair_orphaned_tool_calls(effective_tool_names, config)
                except Exception:
                    pass
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
                    from notifications import notify
                    notify(
                        title="⏹️ Task Stopped",
                        message=f"{task['name']} was stopped.",
                        sound="workflow",
                        icon="⏹️",
                    )
                return  # skip delivery, skip delete_after_run

            # ── Handle paused task (waiting for approval) ─────────────
            if paused:
                try:
                    from buddy.events import BuddyEventType, emit_buddy_event
                    emit_buddy_event(
                        BuddyEventType.APPROVAL_NEEDED,
                        source="tasks",
                        payload={"task_id": task_id, "thread_id": thread_id, "label": "Needs approval"},
                    )
                except Exception:
                    logger.debug("Buddy approval event failed", exc_info=True)
                _finish_run(run_id, "paused",
                            status_message="Waiting for approval")
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

            final_status = ("completed_delivery_failed"
                            if delivery_status == "delivery_failed"
                            else "completed")
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
                from notifications import notify
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
                from tools.browser_tool import get_session_manager as _get_bsm
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
    from threads import _save_thread_meta, _set_thread_model_override

    thread_id = task.get("persistent_thread_id") or uuid.uuid4().hex[:12]
    if not task.get("notify_only"):
        thread_name = (
            f"\u26a1 {task['name']} \u2014 "
            f"{datetime.now().strftime('%b %d, %I:%M %p')}"
        )
        _save_thread_meta(thread_id, thread_name)
    if task.get("model_override"):
        _set_thread_model_override(thread_id, task["model_override"])
    return thread_id


def _on_task_fire(task_id: str) -> None:
    """Callback invoked by APScheduler when a task's trigger fires."""
    from tools import registry as tool_registry

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


def sync_all_jobs() -> None:
    """(Re-)sync every task to the APScheduler job store."""
    tasks = list_tasks()
    for task in tasks:
        _sync_job(task)
    logger.info("Synced %d task(s) to APScheduler", len(tasks))


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

    from tools import registry as tool_registry

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

_VALID_SAFETY_MODES = ("block", "approve", "allow_all")


def get_global_safety_mode() -> str:
    """Return the global default safety mode for new tasks."""
    try:
        with open(_RETRY_CONFIG_PATH) as f:
            mode = json.load(f).get("safety_mode", "block")
            return mode if mode in _VALID_SAFETY_MODES else "block"
    except (FileNotFoundError, json.JSONDecodeError):
        return "block"


def set_global_safety_mode(mode: str) -> None:
    """Save the global safety mode setting."""
    if mode not in _VALID_SAFETY_MODES:
        raise ValueError(f"Invalid safety mode: {mode!r}. Must be one of {_VALID_SAFETY_MODES}")
    data = {}
    try:
        with open(_RETRY_CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    data["safety_mode"] = mode
    with open(_RETRY_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_task_safety_mode(task: dict) -> str:
    """Return the effective safety mode for a task.

    Uses the task's own setting if present, otherwise falls back to
    the global default.
    """
    mode = task.get("safety_mode")
    if mode and mode in _VALID_SAFETY_MODES:
        return mode
    return get_global_safety_mode()


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
    from tools import registry as tool_registry

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

def _save_pipeline_state(
    run_id: str,
    task_id: str,
    thread_id: str,
    current_step_index: int,
    step_outputs: dict,
    config: dict,
    resume_token: str | None = None,
    status: str = "running",
    graph_interrupted: bool = False,
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
        extra_vals.append("true")
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


def _update_pipeline_status(run_id: str, status: str) -> None:
    """Update the status of a pipeline state entry."""
    conn = _get_conn()
    conn.execute(
        "UPDATE pipeline_state SET status = ?, updated_at = ? WHERE run_id = ?",
        (status, datetime.now().isoformat(), run_id),
    )
    conn.commit()
    conn.close()


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


# ── Approval Request Management ─────────────────────────────────────────────

def create_approval_request(
    run_id: str,
    task_id: str,
    step_id: str,
    message: str,
    channel: str | None = None,
    timeout_minutes: int = 30,
) -> tuple[str, str]:
    """Create an approval request and return ``(resume_token, request_id)``."""
    req_id = uuid.uuid4().hex[:12]
    resume_token = uuid.uuid4().hex
    timeout_at = None
    if timeout_minutes > 0:
        timeout_at = (datetime.now() + timedelta(minutes=timeout_minutes)).isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO approval_requests "
        "(id, run_id, task_id, step_id, resume_token, message, channel, "
        "status, requested_at, timeout_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (req_id, run_id, task_id, step_id, resume_token, message,
         channel, datetime.now().isoformat(), timeout_at),
    )
    conn.commit()
    conn.close()
    _emit_buddy_approval_event(
        "needed",
        run_id=run_id,
        task_id=task_id,
        step_id=step_id,
        approval_id=req_id,
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
    label: str = "",
    message: str = "",
) -> None:
    try:
        from buddy.events import BuddyEventType, emit_buddy_event
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
                "label": label or message or status.replace("_", " ").title(),
                "message": message,
            },
        )
    except Exception:
        logger.debug("Buddy approval event failed", exc_info=True)


def get_pending_approvals() -> list[dict]:
    """Return all pending approval requests."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT a.*, t.name AS task_name, t.icon AS task_icon "
        "FROM approval_requests a "
        "LEFT JOIN tasks t ON a.task_id = t.id "
        "WHERE a.status = 'pending' "
        "ORDER BY a.requested_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
            label="Approval timed out",
            message=str(r.get("message") or ""),
        )
        return False

    new_status = "approved" if approved else "denied"
    conn.execute(
        "UPDATE approval_requests SET status = ?, responded_at = ?, response_note = ? "
        "WHERE resume_token = ?",
        (new_status, datetime.now().isoformat(), note, resume_token),
    )
    conn.commit()

    approval_id = dict(row)["id"]
    conn.close()

    _emit_buddy_approval_event(
        new_status,
        run_id=str(r.get("run_id") or ""),
        task_id=str(r.get("task_id") or ""),
        step_id=str(r.get("step_id") or ""),
        approval_id=approval_id,
        label="Approved" if approved else "Denied",
        message=str(r.get("message") or ""),
    )

    # Update all channel messages (cross-channel resolution)
    _resolve_approval_on_channels(approval_id, new_status, source_channel=source)

    # Resume the pipeline — for graph-interrupted steps, denial resumes
    # the graph with approved=False so the tool returns "cancelled" and
    # the step (and subsequent steps) can still complete.
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
            label="Approval timed out",
            message=str(r.get("message") or ""),
        )
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
    from agent import (
        resume_invoke_agent, TaskStoppedError,
        _background_workflow_var, _safety_mode_var, _persistent_thread_var,
    )

    run_id = state["run_id"]
    task_id = state["task_id"]
    config = state.get("config", {})
    step_outputs = state.get("step_outputs", {})
    steps = task.get("steps") or []
    total = len(steps)
    paused_step = steps[paused_step_index] if paused_step_index < total else {}
    step_id = paused_step.get("id", f"step_{paused_step_index + 1}")
    safety_mode = get_task_safety_mode(task)
    effective_tool_names = enabled_tool_names

    # Clear the "(paused)" suffix from the thread name
    from threads import _save_thread_meta, _list_threads as _lt
    if any(t[0] == thread_id for t in _lt()):
        _ts = datetime.now().strftime('%b %d, %I:%M %p')
        _save_thread_meta(thread_id, f"⚡ {task['name']} — {_ts}")

    def _run():
        _background_workflow_var.set(True)
        _safety_mode_var.set(safety_mode)
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
            elif safety_mode == "block":
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
            elif safety_mode == "allow_all":
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
                from notifications import notify
                notify(
                    title="⏸️ Approval Required",
                    message=f"{task['name']}: {approval_msg}",
                    sound="workflow",
                    icon="⏸️",
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
            final_status = ("completed_delivery_failed"
                            if delivery_status == "delivery_failed"
                            else "completed")
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
    from tools import registry as tool_registry
    from threads import _save_thread_meta, _list_threads

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
        from notifications import notify
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
    from threads import _save_thread_meta, _list_threads
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
    from agent import invoke_agent, TaskStoppedError

    token = _subtask_depth_var.set(depth)
    try:
        steps = child_task.get("steps") or _prompts_to_steps(
            child_task.get("prompts") or []
        )
        if not steps:
            return None

        # Apply child task's safety mode
        child_safety = get_task_safety_mode(child_task)
        effective_tools = tool_names

        config = {
            "configurable": {
                "thread_id": thread_id,
            },
            "recursion_limit": parent_config.get("recursion_limit", 50),
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
                    # Subtasks do not support approval flow — if the
                    # agent triggered an interrupt(), handle it inline.
                    if isinstance(result, dict) and result.get("type") == "interrupt":
                        child_safety = get_task_safety_mode(child_task)
                        if child_safety == "allow_all":
                            from agent import resume_invoke_agent
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
                            from agent import resume_invoke_agent
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
                    from notifications import notify as _notify
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
        from agent import invoke_agent

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
        config = {
            "configurable": {"thread_id": f"condition-eval-{uuid.uuid4().hex[:8]}"},
            "recursion_limit": 5,
        }
        # No tools needed — this is a pure yes/no reasoning call.
        result = invoke_agent(full_prompt, [], config)
        if result:
            answer = result.strip().lower()
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
            label = f"{icon} Sub-agent"
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
                "message": "Review the research brief before Thoth prepares the final shareable report.",
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
        )
        created += 1
    return created


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
        )
    open(_MARKER, "w").close()
    logger.info("Seeded %d default tasks", len(_DEFAULT_TASKS))


# Backward-compat aliases for legacy transition
seed_default_workflows = seed_default_tasks
list_workflows = list_tasks
create_workflow = lambda name, prompts, description="", icon="⚡", schedule=None: create_task(
    name=name, prompts=prompts, description=description, icon=icon, schedule=schedule,
)
update_workflow = update_task
delete_workflow = delete_task
duplicate_workflow = duplicate_task
run_workflow_background = run_task_background
