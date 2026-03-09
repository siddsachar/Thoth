"""In-app Timer tool — quick reminders via APScheduler + desktop notifications."""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from datetime import datetime, timedelta

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

# ── Data directory for timer persistence ─────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_TIMER_DB = str(_DATA_DIR / "timers.sqlite")

# ── Singleton scheduler ─────────────────────────────────────────────────────
_scheduler = None


def _get_scheduler():
    """Return a singleton BackgroundScheduler, starting it if needed."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{_TIMER_DB}")}
    _scheduler = BackgroundScheduler(jobstores=jobstores)
    _scheduler.start()
    return _scheduler


def _timer_callback(label: str):
    """Called when a timer fires."""
    from notifications import notify
    notify(
        title="⏰ Thoth Reminder",
        message=label,
        sound="timer",
        icon="⏰",
    )


# ── Schemas ──────────────────────────────────────────────────────────────────

class _SetTimerInput(BaseModel):
    minutes: float = Field(
        description="Number of minutes from now to fire the timer (e.g. 5, 0.5 for 30 seconds, 30, 60)."
    )
    label: str = Field(
        description="Short label for the reminder (e.g. 'Check the oven', 'Stand up and stretch')."
    )


class _ListTimersInput(BaseModel):
    pass


class _CancelTimerInput(BaseModel):
    timer_id: str = Field(
        description="The ID of the timer to cancel (from list_timers output)."
    )


# ── Tool functions ───────────────────────────────────────────────────────────

def _set_timer(minutes: float, label: str) -> str:
    """Set a timer that fires a desktop notification after the given minutes."""
    if minutes <= 0:
        return "Timer duration must be positive."
    if minutes > 1440:
        return "For reminders longer than 24 hours, use Google Calendar instead."

    scheduler = _get_scheduler()
    fire_at = datetime.now() + timedelta(minutes=minutes)
    timer_id = f"timer_{uuid.uuid4().hex[:8]}"

    scheduler.add_job(
        _timer_callback,
        trigger="date",
        run_date=fire_at,
        args=[label],
        id=timer_id,
        name=label,
        replace_existing=True,
    )

    return (
        f"Timer set! ID: {timer_id}\n"
        f"Label: {label}\n"
        f"Fires at: {fire_at.strftime('%I:%M:%S %p')} "
        f"({minutes:.1f} minutes from now)"
    )


def _list_timers() -> str:
    """List all active (pending) timers."""
    scheduler = _get_scheduler()
    jobs = scheduler.get_jobs()

    if not jobs:
        return "No active timers."

    timers = []
    for job in jobs:
        remaining = (job.next_run_time.replace(tzinfo=None) - datetime.now()).total_seconds()
        if remaining < 0:
            continue
        mins_left = remaining / 60
        timers.append({
            "id": job.id,
            "label": job.name,
            "fires_at": job.next_run_time.strftime("%I:%M:%S %p"),
            "minutes_remaining": round(mins_left, 1),
        })

    if not timers:
        return "No active timers."
    return json.dumps(timers, indent=2)


def _cancel_timer(timer_id: str) -> str:
    """Cancel a timer by its ID."""
    scheduler = _get_scheduler()
    try:
        scheduler.remove_job(timer_id)
        return f"Timer '{timer_id}' cancelled."
    except Exception:
        return f"Timer '{timer_id}' not found. Use list_timers to see active timers."


# ── Tool class ───────────────────────────────────────────────────────────────

class TimerTool(BaseTool):

    @property
    def name(self) -> str:
        return "timer"

    @property
    def display_name(self) -> str:
        return "⏰ Timer"

    @property
    def description(self) -> str:
        return (
            "Set quick in-app timers that show desktop notifications. "
            "Use for short reminders (minutes to hours). For day-level "
            "reminders or scheduled events, use Google Calendar instead."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def as_langchain_tools(self) -> list:
        # Ensure the scheduler is running whenever tools are loaded
        _get_scheduler()

        return [
            StructuredTool.from_function(
                func=_set_timer,
                name="set_timer",
                description=(
                    "Set a quick timer that shows a desktop notification after "
                    "the specified number of minutes. Max 24 hours (1440 minutes). "
                    "For longer reminders, use create_calendar_event instead."
                ),
                args_schema=_SetTimerInput,
            ),
            StructuredTool.from_function(
                func=_list_timers,
                name="list_timers",
                description="List all active (pending) timers with their IDs, labels, and remaining time.",
                args_schema=_ListTimersInput,
            ),
            StructuredTool.from_function(
                func=_cancel_timer,
                name="cancel_timer",
                description="Cancel an active timer by its ID. Use list_timers to find the ID.",
                args_schema=_CancelTimerInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return "Use set_timer, list_timers, or cancel_timer instead."


registry.register(TimerTool())
