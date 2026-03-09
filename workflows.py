"""Workflows — reusable prompt sequences with optional scheduling.

A *workflow* is a named list of prompts executed sequentially in a fresh
thread.  Each prompt sees the conversation history from earlier steps, so
prompt #2 can reference the output of prompt #1.

Workflows can be triggered manually (tile click) or on a recurring
schedule (daily / weekly / custom interval).  Scheduled runs execute in
the background via ``invoke_agent`` and fire a desktop notification on
completion.

Storage: SQLite at ``~/.thoth/workflows.db``.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Persistence ──────────────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = str(_DATA_DIR / "workflows.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            icon        TEXT DEFAULT '⚡',
            prompts     TEXT NOT NULL,
            schedule    TEXT,
            enabled     INTEGER DEFAULT 1,
            last_run    TEXT,
            created_at  TEXT NOT NULL,
            sort_order  INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id          TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            thread_id   TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT DEFAULT 'running',
            steps_total INTEGER DEFAULT 0,
            steps_done  INTEGER DEFAULT 0,
            FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()


_init_db()


# ── Template Variables ───────────────────────────────────────────────────────

def expand_template_vars(prompt: str) -> str:
    """Replace ``{{variable}}`` placeholders with current values."""
    now = datetime.now()
    replacements = {
        "date": now.strftime("%B %d, %Y"),
        "day": now.strftime("%A"),
        "time": now.strftime("%I:%M %p"),
        "month": now.strftime("%B"),
        "year": str(now.year),
    }
    result = prompt
    for key, value in replacements.items():
        result = result.replace("{{" + key + "}}", value)
    return result


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_workflow(
    name: str,
    prompts: list[str],
    description: str = "",
    icon: str = "⚡",
    schedule: str | None = None,
) -> str:
    """Create a new workflow and return its ID."""
    wf_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO workflows (id, name, description, icon, prompts, schedule, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (wf_id, name, description, icon, json.dumps(prompts), schedule, now),
    )
    conn.commit()
    conn.close()
    return wf_id


def get_workflow(wf_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (wf_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_dict(row)


def list_workflows() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM workflows ORDER BY sort_order, created_at"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def update_workflow(wf_id: str, **kwargs) -> None:
    """Update workflow fields.  Accepted keys: name, description, icon,
    prompts (list[str]), schedule, enabled, sort_order."""
    conn = _get_conn()
    for key, value in kwargs.items():
        if key == "prompts":
            value = json.dumps(value)
        if key in ("name", "description", "icon", "prompts", "schedule",
                    "enabled", "sort_order", "last_run"):
            conn.execute(
                f"UPDATE workflows SET {key} = ? WHERE id = ?",
                (value, wf_id),
            )
    conn.commit()
    conn.close()


def delete_workflow(wf_id: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
    conn.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (wf_id,))
    conn.commit()
    conn.close()


def duplicate_workflow(wf_id: str) -> str | None:
    """Clone a workflow and return the new ID."""
    wf = get_workflow(wf_id)
    if not wf:
        return None
    return create_workflow(
        name=f"{wf['name']} (copy)",
        prompts=wf["prompts"],
        description=wf["description"],
        icon=wf["icon"],
        schedule=None,  # don't copy schedule
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["prompts"] = json.loads(d["prompts"])
    return d


# ── Run History ──────────────────────────────────────────────────────────────

def _record_run_start(workflow_id: str, thread_id: str, steps_total: int) -> str:
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO workflow_runs (id, workflow_id, thread_id, started_at, "
        "status, steps_total, steps_done) VALUES (?, ?, ?, ?, 'running', ?, 0)",
        (run_id, workflow_id, thread_id, now, steps_total),
    )
    conn.commit()
    conn.close()
    return run_id


def _update_run_progress(run_id: str, steps_done: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE workflow_runs SET steps_done = ? WHERE id = ?",
        (steps_done, run_id),
    )
    conn.commit()
    conn.close()


def _finish_run(run_id: str, status: str = "completed") -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE workflow_runs SET status = ?, finished_at = ? WHERE id = ?",
        (status, datetime.now().isoformat(), run_id),
    )
    conn.commit()
    conn.close()


def get_run_history(workflow_id: str, limit: int = 5) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM workflow_runs WHERE workflow_id = ? "
        "ORDER BY started_at DESC LIMIT ?",
        (workflow_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]



# ── Background Execution Engine ──────────────────────────────────────────────
# Tracks in-flight runs so the UI can show progress indicators.

_active_runs: dict[str, dict] = {}  # thread_id -> {workflow_id, run_id, step, total, name}
_active_lock = threading.Lock()


def get_running_workflows() -> dict[str, dict]:
    """Return ``{thread_id: {workflow_id, run_id, step, total, name}}``
    for all in-flight workflow executions."""
    with _active_lock:
        return dict(_active_runs)


def run_workflow_background(
    workflow_id: str,
    thread_id: str,
    enabled_tool_names: list[str],
    start_step: int = 0,
    notification: bool = True,
) -> None:
    """Execute a workflow in a background thread.

    Each prompt is sent to the agent via ``invoke_agent`` sequentially.
    The thread's messages accumulate in the LangGraph checkpointer, so
    the user can click into the thread at any time to see progress.

    Parameters
    ----------
    start_step : int
        Prompt index to resume from (0-based).  Used when the user watched
        the first step live and then navigated away.
    notification : bool
        Show a desktop notification when the workflow finishes.
    """
    wf = get_workflow(workflow_id)
    if not wf:
        return

    prompts = wf["prompts"]
    total = len(prompts)
    run_id = _record_run_start(workflow_id, thread_id, total)

    def _run():
        from agent import invoke_agent
        from threads import _save_thread_meta

        with _active_lock:
            _active_runs[thread_id] = {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "step": start_step,
                "total": total,
                "name": wf["name"],
            }

        try:
            from agent import _tlocal
            _tlocal.background_workflow = True

            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 25,
            }
            for i in range(start_step, total):
                with _active_lock:
                    _active_runs[thread_id]["step"] = i

                prompt = expand_template_vars(prompts[i])

                try:
                    invoke_agent(prompt, enabled_tool_names, config)
                except Exception as exc:
                    logger.error(
                        "Workflow %s step %d failed: %s", wf["name"], i + 1, exc
                    )
                    # Clean up orphaned tool_calls so subsequent steps
                    # don't fail with INVALID_CHAT_HISTORY
                    try:
                        from agent import repair_orphaned_tool_calls
                        repair_orphaned_tool_calls(enabled_tool_names, config)
                    except Exception:
                        pass

                _update_run_progress(run_id, i + 1)

            _finish_run(run_id, "completed")
            update_workflow(workflow_id, last_run=datetime.now().isoformat())

            # Bump thread timestamp so it floats to top in sidebar
            thread_name = f"⚡ {wf['name']} — {datetime.now().strftime('%b %d, %I:%M %p')}"
            _save_thread_meta(thread_id, thread_name)

            if notification:
                from notifications import notify
                notify(
                    title="⚡ Workflow Complete",
                    message=f"{wf['name']} finished ({total} step{'s' if total != 1 else ''}).",
                    sound="workflow",
                    icon="⚡",
                )
        except Exception as exc:
            logger.error("Workflow %s crashed: %s", wf["name"], exc)
            _finish_run(run_id, "failed")
        finally:
            with _active_lock:
                _active_runs.pop(thread_id, None)

    t = threading.Thread(target=_run, daemon=True, name=f"workflow-{workflow_id}")
    t.start()





# ── Scheduler ────────────────────────────────────────────────────────────────
# A background thread that checks once per minute for due workflows.

_scheduler_started = False


def _parse_schedule(schedule: str | None) -> dict | None:
    """Parse schedule strings into a dict.

    Formats:
        "daily:HH:MM"            → run every day at HH:MM
        "weekly:DAY:HH:MM"       → run every week on DAY at HH:MM
        "interval:HOURS"          → run every N hours
    """
    if not schedule:
        return None
    parts = schedule.split(":")
    if len(parts) < 2:
        return None

    kind = parts[0].lower()
    try:
        if kind == "daily" and len(parts) >= 3:
            return {"kind": "daily", "hour": int(parts[1]), "minute": int(parts[2])}
        elif kind == "weekly" and len(parts) >= 4:
            return {
                "kind": "weekly",
                "day": parts[1].lower(),
                "hour": int(parts[2]),
                "minute": int(parts[3]),
            }
        elif kind == "interval" and len(parts) >= 2:
            return {"kind": "interval", "hours": float(parts[1])}
    except (ValueError, IndexError):
        pass
    return None


_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _is_due(wf: dict) -> bool:
    """Check if a scheduled workflow should run now."""
    sched = _parse_schedule(wf.get("schedule"))
    if not sched or not wf.get("enabled", True):
        return False

    now = datetime.now()

    # Must not have run in the last 55 minutes (prevent double-firing)
    last_run = wf.get("last_run")
    if last_run:
        try:
            lr = datetime.fromisoformat(last_run)
            if (now - lr) < timedelta(minutes=55):
                return False
        except (ValueError, TypeError):
            pass

    kind = sched["kind"]
    if kind == "daily":
        return now.hour == sched["hour"] and now.minute == sched["minute"]
    elif kind == "weekly":
        target_day = _DAY_MAP.get(sched["day"])
        if target_day is None:
            return False
        return (
            now.weekday() == target_day
            and now.hour == sched["hour"]
            and now.minute == sched["minute"]
        )
    elif kind == "interval":
        if not last_run:
            return True  # Never run before → run now
        try:
            lr = datetime.fromisoformat(last_run)
            return (now - lr) >= timedelta(hours=sched["hours"])
        except (ValueError, TypeError):
            return True
    return False


def _scheduler_loop() -> None:
    """Background loop — checks every 60 s for due workflows."""
    import time

    from tools import registry as tool_registry

    while True:
        try:
            workflows = list_workflows()
            for wf in workflows:
                if _is_due(wf):
                    logger.info("Scheduler triggering workflow: %s", wf["name"])
                    # Mark last_run immediately to prevent re-triggering
                    # while the workflow is still running
                    update_workflow(wf["id"], last_run=datetime.now().isoformat())
                    thread_id = uuid.uuid4().hex[:12]
                    enabled = [t.name for t in tool_registry.get_enabled_tools()]
                    run_workflow_background(
                        wf["id"],
                        thread_id,
                        enabled,
                        notification=True,
                    )
        except Exception as exc:
            logger.debug("Scheduler tick error: %s", exc)

        time.sleep(60)


def start_workflow_scheduler() -> None:
    """Start the scheduler background thread (idempotent)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="workflow-scheduler")
    t.start()
    logger.info("Workflow scheduler started")


# ── Default Templates ────────────────────────────────────────────────────────

_DEFAULT_WORKFLOWS = [
    {
        "name": "Daily Briefing",
        "description": "News, weather, and today's calendar",
        "icon": "📰",
        "prompts": [
            "Give me a brief summary of the top 5 news stories today.",
            "What's the weather forecast for today and tomorrow?",
            "What events do I have on my calendar for {{date}}?",
        ],
    },
    {
        "name": "Research Summary",
        "description": "Deep-dive into a topic with sources",
        "icon": "🔬",
        "prompts": [
            "Search the web for the latest developments in artificial intelligence this week. "
            "Find at least 3-4 notable stories or breakthroughs.",
            "Now summarize your findings into a well-structured briefing with bullet points "
            "and source citations for each item.",
        ],
    },
    {
        "name": "Email Digest",
        "description": "Check and summarize unread emails",
        "icon": "📧",
        "prompts": [
            "Check my Gmail inbox for any unread or recent emails from today.",
            "Summarize each email in 1-2 sentences, grouped by priority "
            "(action required vs. informational). List the sender and subject for each.",
        ],
    },
    {
        "name": "Weekly Review",
        "description": "Recap of the past week's events and tasks",
        "icon": "📋",
        "prompts": [
            "What events did I have on my calendar this past week (last 7 days)?",
            "Based on these events, write a short weekly review summarizing what I was busy "
            "with this week. Highlight any patterns and suggest priorities for next week.",
        ],
    },
]


def seed_default_workflows() -> None:
    """Insert default workflow templates if the DB is empty."""
    existing = list_workflows()
    if existing:
        return  # User already has workflows — don't overwrite
    for wf in _DEFAULT_WORKFLOWS:
        create_workflow(
            name=wf["name"],
            prompts=wf["prompts"],
            description=wf["description"],
            icon=wf["icon"],
        )
    logger.info("Seeded %d default workflows", len(_DEFAULT_WORKFLOWS))
