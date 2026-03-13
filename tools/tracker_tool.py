"""Habit / activity tracker tool — log, query, and analyse recurring events.

Stores trackers and entries in ``~/.thoth/tracker/tracker.db`` (SQLite).
Exposes three sub-tools to the agent:

* **tracker_log** — structured input for logging an entry (auto-creates the
  tracker if it doesn't exist yet).
* **tracker_query** — free-text ``query: str`` for read-only operations:
  list trackers, show history, compute stats, run trend analysis.  When
  results are tabular it also writes a temp CSV so the agent can chain into
  ``create_chart``.
* **tracker_delete** — free-text ``query: str`` for destructive ops (delete
  an entry or a whole tracker).  Gated behind ``interrupt()``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import pathlib
import re
import sqlite3
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

# ── Data directory ───────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_TRACKER_DIR = _DATA_DIR / "tracker"
_TRACKER_DIR.mkdir(parents=True, exist_ok=True)
_EXPORT_DIR = _TRACKER_DIR / "exports"
_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = _TRACKER_DIR / "tracker.db"


# ── Database initialisation ─────────────────────────────────────────────
def _get_db() -> sqlite3.Connection:
    """Return a connection to the tracker database (creates tables on first
    call)."""
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trackers (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            type        TEXT NOT NULL DEFAULT 'boolean',
            unit        TEXT,
            icon        TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            id          TEXT PRIMARY KEY,
            tracker_id  TEXT NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
            timestamp   TEXT NOT NULL,
            value       TEXT NOT NULL DEFAULT 'true',
            notes       TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_tracker
            ON entries(tracker_id, timestamp);
    """)
    return conn


# ── Helpers ──────────────────────────────────────────────────────────────
_NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def _now_iso() -> str:
    return datetime.now().strftime(_NOW_FMT)


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-ish timestamp string."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts!r}")


def _find_tracker(conn: sqlite3.Connection, name: str) -> dict | None:
    """Find a tracker by exact or fuzzy name match."""
    row = conn.execute(
        "SELECT id, name, type, unit, icon, created_at FROM trackers WHERE name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()
    if row:
        return dict(zip(("id", "name", "type", "unit", "icon", "created_at"), row))
    # Fuzzy: substring match
    rows = conn.execute(
        "SELECT id, name, type, unit, icon, created_at FROM trackers WHERE name LIKE ? COLLATE NOCASE",
        (f"%{name}%",),
    ).fetchall()
    if len(rows) == 1:
        return dict(zip(("id", "name", "type", "unit", "icon", "created_at"), rows[0]))
    return None


def _get_all_trackers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, type, unit, icon, created_at FROM trackers ORDER BY name"
    ).fetchall()
    return [dict(zip(("id", "name", "type", "unit", "icon", "created_at"), r)) for r in rows]


def _create_tracker(
    conn: sqlite3.Connection,
    name: str,
    tracker_type: str = "boolean",
    unit: str | None = None,
    icon: str | None = None,
) -> dict:
    tid = str(uuid.uuid4())
    now = _now_iso()
    conn.execute(
        "INSERT INTO trackers (id, name, type, unit, icon, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (tid, name, tracker_type, unit, icon, now),
    )
    conn.commit()
    return {"id": tid, "name": name, "type": tracker_type, "unit": unit, "icon": icon, "created_at": now}


def _log_entry(
    conn: sqlite3.Connection,
    tracker_id: str,
    value: str = "true",
    notes: str | None = None,
    timestamp: str | None = None,
) -> dict:
    eid = str(uuid.uuid4())
    ts = timestamp or _now_iso()
    now = _now_iso()
    conn.execute(
        "INSERT INTO entries (id, tracker_id, timestamp, value, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (eid, tracker_id, ts, value, notes, now),
    )
    conn.commit()
    return {"id": eid, "tracker_id": tracker_id, "timestamp": ts, "value": value, "notes": notes}


def _get_entries(
    conn: sqlite3.Connection,
    tracker_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT id, tracker_id, timestamp, value, notes FROM entries WHERE tracker_id = ?"
    params: list = [tracker_id]
    if since:
        sql += " AND timestamp >= ?"
        params.append(since.strftime(_NOW_FMT))
    if until:
        sql += " AND timestamp <= ?"
        params.append(until.strftime(_NOW_FMT))
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(("id", "tracker_id", "timestamp", "value", "notes"), r)) for r in rows]


# ── Period parsing ───────────────────────────────────────────────────────
_PERIOD_RE = re.compile(r"(\d+)\s*(d|day|days|w|week|weeks|m|month|months|y|year|years)", re.I)

def _parse_period(text: str) -> timedelta | None:
    """Extract a time period from text like '30d', '3 months', etc."""
    m = _PERIOD_RE.search(text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("d"):
        return timedelta(days=n)
    if unit.startswith("w"):
        return timedelta(weeks=n)
    if unit.startswith("m"):
        return timedelta(days=n * 30)
    if unit.startswith("y"):
        return timedelta(days=n * 365)
    return None


def _default_period() -> timedelta:
    return timedelta(days=30)


# ── Export CSV helper ────────────────────────────────────────────────────
def _export_csv(rows: list[dict], label: str) -> str:
    """Write rows to a temp CSV and return the path."""
    # Deterministic filename from label
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60]
    path = _EXPORT_DIR / f"{slug}.csv"
    if not rows:
        return ""
    keys = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    return str(path)


# ── Analysis helpers ─────────────────────────────────────────────────────

def _adherence(entries: list[dict], days: int) -> dict:
    """Boolean tracker adherence: % of days with at least one 'true' entry."""
    dates_with = set()
    for e in entries:
        if e["value"].lower() in ("true", "yes", "1", "done", "taken"):
            try:
                dates_with.add(_parse_ts(e["timestamp"]).date())
            except ValueError:
                pass
    return {
        "days_tracked": len(dates_with),
        "total_days": days,
        "adherence_pct": round(len(dates_with) / max(days, 1) * 100, 1),
    }


def _streaks(entries: list[dict]) -> dict:
    """Compute current and longest consecutive-day streaks (boolean tracker)."""
    dates = set()
    for e in entries:
        if e["value"].lower() in ("true", "yes", "1", "done", "taken"):
            try:
                dates.add(_parse_ts(e["timestamp"]).date())
            except ValueError:
                pass
    if not dates:
        return {"current_streak": 0, "longest_streak": 0}
    sorted_dates = sorted(dates)
    streaks_list = []
    cur = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            cur += 1
        else:
            streaks_list.append(cur)
            cur = 1
    streaks_list.append(cur)
    longest = max(streaks_list)
    # Current streak: count back from today (or the latest entry date if
    # it is today or yesterday — so an evening log still counts).
    today = datetime.now().date()
    start = today if today in dates else sorted_dates[-1]
    # Only treat "latest entry" as current if it is today or yesterday;
    # older data has no active current streak.
    if (today - start).days > 1:
        current = 0
    else:
        current = 0
        d = start
        while d in dates:
            current += 1
            d -= timedelta(days=1)
    return {"current_streak": current, "longest_streak": longest}


def _numeric_stats(entries: list[dict]) -> dict:
    """Mean, median, min, max, std dev for numeric values."""
    vals = []
    for e in entries:
        try:
            vals.append(float(e["value"]))
        except (ValueError, TypeError):
            pass
    if not vals:
        return {}
    result = {
        "count": len(vals),
        "mean": round(statistics.mean(vals), 2),
        "median": round(statistics.median(vals), 2),
        "min": min(vals),
        "max": max(vals),
    }
    if len(vals) >= 2:
        result["std_dev"] = round(statistics.stdev(vals), 2)
    return result


def _frequency(entries: list[dict], days: int) -> dict:
    """Entries per week and per month."""
    total = len(entries)
    weeks = max(days / 7, 1)
    months = max(days / 30, 1)
    return {
        "total_entries": total,
        "per_week": round(total / weeks, 1),
        "per_month": round(total / months, 1),
    }


def _day_of_week_distribution(entries: list[dict]) -> dict:
    """Count entries per day of week."""
    dist = Counter()
    for e in entries:
        try:
            dow = _parse_ts(e["timestamp"]).strftime("%A")
            dist[dow] += 1
        except ValueError:
            pass
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return {d: dist.get(d, 0) for d in dow_order}


def _cycle_estimation(entries: list[dict]) -> dict:
    """Average gap between 'started' entries (for duration trackers)."""
    starts = []
    for e in entries:
        if e["value"].lower() in ("started", "start", "began", "true", "yes"):
            try:
                starts.append(_parse_ts(e["timestamp"]))
            except ValueError:
                pass
    if len(starts) < 2:
        return {"cycles": len(starts), "avg_cycle_days": None}
    starts.sort()
    gaps = [(starts[i] - starts[i - 1]).days for i in range(1, len(starts))]
    return {
        "cycles": len(starts),
        "avg_cycle_days": round(statistics.mean(gaps), 1),
        "min_cycle_days": min(gaps),
        "max_cycle_days": max(gaps),
    }


def _co_occurrence(
    conn: sqlite3.Connection,
    tracker_a_id: str,
    tracker_b_id: str,
    window_days: int = 3,
    since: datetime | None = None,
) -> dict:
    """Check if entries in tracker B cluster within ±window_days of tracker A entries."""
    since_str = (since or (datetime.now() - timedelta(days=365))).strftime(_NOW_FMT)
    a_entries = conn.execute(
        "SELECT timestamp FROM entries WHERE tracker_id = ? AND timestamp >= ? ORDER BY timestamp",
        (tracker_a_id, since_str),
    ).fetchall()
    b_entries = conn.execute(
        "SELECT timestamp FROM entries WHERE tracker_id = ? AND timestamp >= ? ORDER BY timestamp",
        (tracker_b_id, since_str),
    ).fetchall()
    if not a_entries or not b_entries:
        return {"matches": 0, "a_total": len(a_entries), "b_total": len(b_entries)}
    a_dates = [_parse_ts(r[0]) for r in a_entries]
    b_dates = [_parse_ts(r[0]) for r in b_entries]
    matches = 0
    for ad in a_dates:
        for bd in b_dates:
            if abs((ad - bd).days) <= window_days:
                matches += 1
                break
    return {
        "matches": matches,
        "a_total": len(a_dates),
        "b_total": len(b_dates),
        "match_pct": round(matches / max(len(a_dates), 1) * 100, 1),
        "window_days": window_days,
    }


# ── tracker_log (structured input) ──────────────────────────────────────

class _TrackerLogInput(BaseModel):
    tracker_name: str = Field(
        description="Name of the tracker (e.g. 'Lexapro', 'Headache', 'Period', 'Exercise')."
    )
    value: str = Field(
        default="true",
        description=(
            "Value to log.  For boolean trackers use 'true'/'false'. "
            "For numeric trackers use a number (e.g. '6').  For duration "
            "trackers use 'started'/'ended'.  For categorical use the "
            "category label (e.g. 'good', 'bad')."
        ),
    )
    tracker_type: str = Field(
        default="boolean",
        description=(
            "Type of tracker — only used when auto-creating a new tracker. "
            "One of: boolean, numeric, duration, categorical."
        ),
    )
    unit: Optional[str] = Field(
        default=None,
        description="Unit for numeric trackers (e.g. 'mg', '1-10', 'hours'). Optional.",
    )
    notes: Optional[str] = Field(
        default=None, description="Optional free-text annotation."
    )
    timestamp: Optional[str] = Field(
        default=None,
        description=(
            "ISO datetime for the entry. Defaults to current time if omitted. "
            "Example: '2026-03-11T08:30:00' or '2026-03-11'."
        ),
    )


def _tracker_log(
    tracker_name: str,
    value: str = "true",
    tracker_type: str = "boolean",
    unit: str | None = None,
    notes: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Log an entry to a tracker, auto-creating the tracker if needed."""
    conn = _get_db()
    tracker = _find_tracker(conn, tracker_name)
    created_new = False
    if tracker is None:
        tracker = _create_tracker(conn, tracker_name, tracker_type, unit)
        created_new = True
    entry = _log_entry(conn, tracker["id"], value, notes, timestamp)
    ts_display = entry["timestamp"]
    parts = [f"✅ Logged **{tracker['name']}**"]
    if value.lower() not in ("true", "yes"):
        parts[0] += f" = {value}"
    if tracker.get("unit"):
        parts[0] += f" {tracker['unit']}"
    parts.append(f"at {ts_display}")
    if notes:
        parts.append(f"({notes})")
    if created_new:
        parts.append(f"\n📋 New tracker '{tracker['name']}' created (type: {tracker_type})")
    return " ".join(parts)


# ── tracker_query (free-text query) ──────────────────────────────────────

def _tracker_query(query: str) -> str:
    """Handle read-only tracker queries: list, history, stats, trends."""
    conn = _get_db()
    q = query.lower().strip()

    # ── List all trackers ────────────────────────────────────────────────
    if any(kw in q for kw in ("list", "all tracker", "what am i tracking", "show tracker", "which tracker")):
        trackers = _get_all_trackers(conn)
        if not trackers:
            return "You don't have any trackers yet.  Tell me something to track and I'll set it up."
        lines = ["📋 **Your trackers:**\n"]
        for t in trackers:
            icon = t["icon"] or "•"
            count = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE tracker_id = ?", (t["id"],)
            ).fetchone()[0]
            lines.append(f"  {icon} **{t['name']}** ({t['type']}) — {count} entries")
        return "\n".join(lines)

    # ── Try to extract tracker name(s) from query ────────────────────────
    trackers = _get_all_trackers(conn)
    matched = []
    for t in trackers:
        if t["name"].lower() in q:
            matched.append(t)
    # Fall back to fuzzy match
    if not matched:
        for t in trackers:
            if any(word in q for word in t["name"].lower().split()):
                matched.append(t)

    # ── Co-occurrence / correlation between two trackers ─────────────────
    if len(matched) >= 2 and any(kw in q for kw in ("correlat", "relate", "connect", "pattern between",
                                                      "co-occur", "link between", "together")):
        period_td = _parse_period(q) or timedelta(days=365)
        since = datetime.now() - period_td
        result = _co_occurrence(conn, matched[0]["id"], matched[1]["id"], window_days=3, since=since)
        return (
            f"📊 **Co-occurrence: {matched[0]['name']} ↔ {matched[1]['name']}**\n"
            f"(within ±{result['window_days']}-day window, last {period_td.days} days)\n\n"
            f"• {matched[0]['name']} entries: {result['a_total']}\n"
            f"• {matched[1]['name']} entries: {result['b_total']}\n"
            f"• Matches (A occurred within window of B): {result['matches']}\n"
            f"• Match rate: {result['match_pct']}%"
        )

    if not matched:
        if not trackers:
            return "No trackers found.  Tell me something to track and I'll set it up."
        names = ", ".join(t["name"] for t in trackers)
        return f"I couldn't identify which tracker you mean.  Your trackers: {names}"

    tracker = matched[0]
    period_td = _parse_period(q) or _default_period()
    since = datetime.now() - period_td
    entries = _get_entries(conn, tracker["id"], since=since)

    if not entries:
        return f"No entries for **{tracker['name']}** in the last {period_td.days} days."

    # ── Trend / analysis keywords ────────────────────────────────────────
    if any(kw in q for kw in ("trend", "analy", "pattern", "insight", "summary", "overview", "report")):
        parts = [f"📊 **{tracker['name']} — Analysis** (last {period_td.days} days)\n"]

        # Type-specific stats
        if tracker["type"] == "boolean":
            adh = _adherence(entries, period_td.days)
            stk = _streaks(entries)
            parts.append(f"**Adherence:** {adh['adherence_pct']}% ({adh['days_tracked']}/{adh['total_days']} days)")
            parts.append(f"**Current streak:** {stk['current_streak']} days")
            parts.append(f"**Longest streak:** {stk['longest_streak']} days")
        elif tracker["type"] == "numeric":
            ns = _numeric_stats(entries)
            if ns:
                parts.append(f"**Mean:** {ns['mean']}  |  **Median:** {ns['median']}")
                parts.append(f"**Range:** {ns['min']} – {ns['max']}")
                if "std_dev" in ns:
                    parts.append(f"**Std dev:** {ns['std_dev']}")
        elif tracker["type"] == "duration":
            ce = _cycle_estimation(entries)
            if ce["avg_cycle_days"]:
                parts.append(f"**Average cycle:** {ce['avg_cycle_days']} days")
                parts.append(f"**Range:** {ce['min_cycle_days']} – {ce['max_cycle_days']} days")
                parts.append(f"**Cycles recorded:** {ce['cycles']}")

        freq = _frequency(entries, period_td.days)
        parts.append(f"\n**Frequency:** {freq['per_week']}/week, {freq['per_month']}/month ({freq['total_entries']} total)")

        dow = _day_of_week_distribution(entries)
        top_day = max(dow, key=dow.get) if dow else "N/A"
        parts.append(f"**Most active day:** {top_day}")

        # Export CSV for charting
        csv_rows = [{"date": e["timestamp"][:10], "value": e["value"], "notes": e.get("notes", "")} for e in entries]
        csv_path = _export_csv(csv_rows, f"{tracker['name']}_analysis_{period_td.days}d")
        if csv_path:
            parts.append(f"\n📁 Data exported to `{csv_path}` — use create_chart to visualise.")

        # Also export day-of-week CSV for charting
        dow_rows = [{"day": d, "count": c} for d, c in dow.items()]
        dow_csv = _export_csv(dow_rows, f"{tracker['name']}_by_day_of_week")
        if dow_csv:
            parts.append(f"📁 Day-of-week data: `{dow_csv}`")

        return "\n".join(parts)

    # ── Stats keywords ───────────────────────────────────────────────────
    if any(kw in q for kw in ("stat", "adherence", "streak", "rate", "compliance", "average", "mean")):
        parts = [f"📈 **{tracker['name']} — Stats** (last {period_td.days} days)\n"]
        if tracker["type"] == "boolean":
            adh = _adherence(entries, period_td.days)
            stk = _streaks(entries)
            parts.append(f"**Adherence:** {adh['adherence_pct']}% ({adh['days_tracked']}/{adh['total_days']} days)")
            parts.append(f"**Current streak:** {stk['current_streak']} days")
            parts.append(f"**Longest streak:** {stk['longest_streak']} days")
        elif tracker["type"] == "numeric":
            ns = _numeric_stats(entries)
            if ns:
                parts.append(f"**Count:** {ns['count']}")
                parts.append(f"**Mean:** {ns['mean']}  |  **Median:** {ns['median']}")
                parts.append(f"**Range:** {ns['min']} – {ns['max']}")
                if "std_dev" in ns:
                    parts.append(f"**Std dev:** {ns['std_dev']}")
        elif tracker["type"] == "duration":
            ce = _cycle_estimation(entries)
            if ce["avg_cycle_days"]:
                parts.append(f"**Average cycle:** {ce['avg_cycle_days']} days")
                parts.append(f"**Range:** {ce['min_cycle_days']} – {ce['max_cycle_days']} days")
        freq = _frequency(entries, period_td.days)
        parts.append(f"\n**Frequency:** {freq['per_week']}/week, {freq['per_month']}/month")
        return "\n".join(parts)

    # ── History (default) ────────────────────────────────────────────────
    limit_match = re.search(r"last\s+(\d+)", q)
    limit = int(limit_match.group(1)) if limit_match else min(len(entries), 20)
    shown = entries[:limit]

    lines = [f"📋 **{tracker['name']}** — last {len(shown)} entries (of {len(entries)} in {period_td.days}d):\n"]
    for e in shown:
        val_str = e["value"]
        if val_str.lower() in ("true", "yes"):
            val_str = "✅"
        elif val_str.lower() in ("false", "no"):
            val_str = "❌"
        note_str = f" — {e['notes']}" if e.get("notes") else ""
        lines.append(f"  • {e['timestamp'][:16]}  {val_str}{note_str}")

    # Export CSV for charting
    csv_rows = [{"date": e["timestamp"][:10], "value": e["value"], "notes": e.get("notes", "")} for e in entries]
    csv_path = _export_csv(csv_rows, f"{tracker['name']}_history_{period_td.days}d")
    if csv_path:
        lines.append(f"\n📁 Full data exported to `{csv_path}` — use create_chart to visualise.")

    return "\n".join(lines)


# ── tracker_delete (free-text, destructive) ──────────────────────────────

def _tracker_delete(query: str) -> str:
    """Delete a tracker or specific entries.  Gated by interrupt()."""
    conn = _get_db()
    q = query.lower().strip()

    # Delete entire tracker
    if any(kw in q for kw in ("tracker", "stop tracking", "remove tracker", "delete tracker")):
        trackers = _get_all_trackers(conn)
        matched = None
        for t in trackers:
            if t["name"].lower() in q:
                matched = t
                break
        if not matched:
            for t in trackers:
                if any(word in q for word in t["name"].lower().split()):
                    matched = t
                    break
        if not matched:
            names = ", ".join(t["name"] for t in trackers) if trackers else "none"
            return f"Couldn't identify which tracker to delete.  Your trackers: {names}"
        count = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE tracker_id = ?", (matched["id"],)
        ).fetchone()[0]
        conn.execute("DELETE FROM trackers WHERE id = ?", (matched["id"],))
        conn.commit()
        return f"🗑️ Deleted tracker **{matched['name']}** and its {count} entries."

    # Delete specific entry (last / most recent) — also the default when a
    # tracker name is matched but no explicit "tracker" keyword is present.
    trackers = _get_all_trackers(conn)
    matched = None
    for t in trackers:
        if t["name"].lower() in q:
            matched = t
            break
    if not matched:
        for t in trackers:
            if any(word in q for word in t["name"].lower().split()):
                matched = t
                break
    if not matched:
        return "Couldn't identify which tracker's entry to delete.  Try 'delete tracker X' or 'delete last entry for X'."
    last = conn.execute(
        "SELECT id, timestamp, value FROM entries WHERE tracker_id = ? ORDER BY timestamp DESC LIMIT 1",
        (matched["id"],),
    ).fetchone()
    if not last:
        return f"No entries to delete for **{matched['name']}**."
    conn.execute("DELETE FROM entries WHERE id = ?", (last[0],))
    conn.commit()
    return f"🗑️ Deleted most recent entry for **{matched['name']}** ({last[1][:16]}, value: {last[2]})."


# ── Tool class ───────────────────────────────────────────────────────────

class TrackerTool(BaseTool):

    @property
    def name(self) -> str:
        return "tracker"

    @property
    def display_name(self) -> str:
        return "📋 Habit Tracker"

    @property
    def description(self) -> str:
        return (
            "Track recurring activities, habits, symptoms, medications, "
            "and health events.  Log entries, view history, compute streaks "
            "and adherence, and analyse trends over time."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def destructive_tool_names(self) -> set[str]:
        return {"tracker_delete"}

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_tracker_log,
                name="tracker_log",
                description=(
                    "Log an entry to a habit/activity tracker.  Auto-creates "
                    "the tracker if it doesn't exist yet.  Use for medications, "
                    "symptoms, habits, periods, exercise, mood, sleep, or any "
                    "recurring activity.  The tracker_type is only needed when "
                    "creating a new tracker (boolean, numeric, duration, or "
                    "categorical)."
                ),
                args_schema=_TrackerLogInput,
            ),
            StructuredTool.from_function(
                func=_tracker_query,
                name="tracker_query",
                description=(
                    "Query tracked activity data.  Handles: listing all "
                    "trackers, viewing entry history, computing stats "
                    "(adherence, streaks, averages), running trend analysis, "
                    "and checking co-occurrence between two trackers.  "
                    "Use natural language: 'list my trackers', 'show my "
                    "Lexapro history this month', 'headache trends last 90 "
                    "days', 'do headaches correlate with my period?'.  "
                    "Returns data as text and exports CSV files for charting."
                ),
            ),
            StructuredTool.from_function(
                func=_tracker_delete,
                name="tracker_delete",
                description=(
                    "Delete a tracker or its entries.  Examples: 'delete "
                    "tracker Weight', 'remove last entry for Lexapro', "
                    "'stop tracking Exercise'."
                ),
            ),
        ]

    def execute(self, query: str) -> str:
        return "Use tracker_log, tracker_query, or tracker_delete."


registry.register(TrackerTool())
