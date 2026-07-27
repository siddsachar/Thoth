"""Durable Agent Run persistence.

Agent runs are the shared status/log primitive for workflows, chat-spawned
subagents, goals, and future monitor/scheduled executions. This module is
intentionally UI-neutral and stores rows in the existing tasks.db.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence


_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY_PATH: str | None = None

AGENT_RUN_KINDS = {"workflow", "subagent", "goal", "scheduled", "monitor"}
AGENT_RUN_STATUSES = {
    "queued",
    "running",
    "waiting_approval",
    "waiting_user",
    "paused",
    "interrupted",
    "completed",
    "completed_delivery_failed",
    "failed",
    "stopped",
    "blocked",
    "timed_out",
    "cancelled",
}
AGENT_EVENT_VISIBILITIES = {
    "internal",
    "log",
    "parent_summary",
    "user_visible",
}
TERMINAL_STATUSES = {
    "completed",
    "completed_delivery_failed",
    "failed",
    "stopped",
    "blocked",
    "timed_out",
    "cancelled",
}

DEFAULT_AGENT_SETTINGS: dict[str, Any] = {
    "max_iterations": 90,
    "max_spawn_depth": 1,
    "max_concurrent_children": 3,
    "max_active_children_global": 8,
    "child_timeout_seconds": 0,
    "default_context_mode": "focused",
    "default_workspace_mode": "single_writer",
    "goal_max_turns": 20,
}

_CREATE_TABLE_SQL: dict[str, str] = {
    "agent_runs": """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT 'subagent',
            status TEXT NOT NULL DEFAULT 'queued',
            status_message TEXT DEFAULT '',
            parent_run_id TEXT DEFAULT '',
            root_run_id TEXT DEFAULT '',
            parent_thread_id TEXT DEFAULT '',
            parent_message_id TEXT DEFAULT '',
            thread_id TEXT DEFAULT '',
            task_id TEXT DEFAULT '',
            goal_id TEXT DEFAULT '',
            depth INTEGER DEFAULT 0,
            profile_id TEXT DEFAULT '',
            profile_slug TEXT DEFAULT '',
            profile_display_name TEXT DEFAULT '',
            profile_snapshot_json TEXT DEFAULT '{}',
            display_name TEXT DEFAULT '',
            prompt TEXT DEFAULT '',
            context_mode TEXT DEFAULT '',
            context_summary TEXT DEFAULT '',
            model_override TEXT DEFAULT '',
            tools_override TEXT DEFAULT '[]',
            skills_override TEXT DEFAULT '[]',
            approval_mode TEXT DEFAULT '',
            workspace_id TEXT DEFAULT '',
            workspace_path TEXT DEFAULT '',
            workspace_mode TEXT DEFAULT '',
            write_lock_key TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT DEFAULT '',
            finished_at TEXT DEFAULT '',
            last_event_at TEXT DEFAULT '',
            timeout_at TEXT DEFAULT '',
            max_turns INTEGER DEFAULT 0,
            turns_used INTEGER DEFAULT 0,
            model_iterations_max INTEGER DEFAULT 0,
            model_iterations_used INTEGER DEFAULT 0,
            terminal_reason TEXT DEFAULT '',
            heartbeat_at TEXT DEFAULT '',
            active_seconds REAL DEFAULT 0,
            token_budget INTEGER DEFAULT 0,
            tokens_used INTEGER DEFAULT 0,
            cost_estimate REAL DEFAULT 0,
            summary TEXT DEFAULT '',
            result_json TEXT DEFAULT '{}',
            error TEXT DEFAULT '',
            settings_snapshot_json TEXT DEFAULT '{}',
            resume_state_json TEXT DEFAULT '{}',
            stop_requested INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """,
    "agent_run_events": """
        CREATE TABLE IF NOT EXISTS agent_run_events (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            visibility TEXT DEFAULT 'log',
            payload_json TEXT DEFAULT '{}'
        )
    """,
    "agent_run_edges": """
        CREATE TABLE IF NOT EXISTS agent_run_edges (
            parent_run_id TEXT NOT NULL,
            child_run_id TEXT NOT NULL,
            relation TEXT DEFAULT 'delegated',
            created_at TEXT NOT NULL,
            PRIMARY KEY (parent_run_id, child_run_id, relation)
        )
    """,
    "agent_write_locks": """
        CREATE TABLE IF NOT EXISTS agent_write_locks (
            lock_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            parent_run_id TEXT DEFAULT '',
            thread_id TEXT DEFAULT '',
            workspace_id TEXT DEFAULT '',
            workspace_path TEXT DEFAULT '',
            acquired_at TEXT NOT NULL,
            expires_at TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}'
        )
    """,
    "thread_goals": """
        CREATE TABLE IF NOT EXISTS thread_goals (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            turns_used INTEGER DEFAULT 0,
            max_turns INTEGER DEFAULT 20,
            token_budget INTEGER DEFAULT 0,
            tokens_used INTEGER DEFAULT 0,
            last_verdict TEXT DEFAULT '',
            last_reason TEXT DEFAULT '',
            last_progress TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '[]',
            subgoals_json TEXT DEFAULT '[]',
            blockers_json TEXT DEFAULT '[]',
            last_blocker TEXT DEFAULT '',
            blocker_count INTEGER DEFAULT 0,
            verifier_failures INTEGER DEFAULT 0,
            active_run_id TEXT DEFAULT '',
            last_turn_id TEXT DEFAULT '',
            continuation_key TEXT DEFAULT ''
        )
    """,
    "agent_orchestrations": """
        CREATE TABLE IF NOT EXISTS agent_orchestrations (
            id TEXT PRIMARY KEY,
            parent_thread_id TEXT NOT NULL,
            parent_generation_id TEXT NOT NULL,
            parent_run_id TEXT NOT NULL DEFAULT '',
            root_objective TEXT NOT NULL,
            status TEXT NOT NULL,
            model_ref TEXT NOT NULL,
            approval_mode TEXT NOT NULL,
            runtime_surface TEXT NOT NULL,
            required_total INTEGER NOT NULL DEFAULT 0,
            optional_total INTEGER NOT NULL DEFAULT 0,
            acknowledgement_sent INTEGER NOT NULL DEFAULT 0,
            continuation_state_json TEXT NOT NULL DEFAULT '{}',
            delivery_context_json TEXT NOT NULL DEFAULT '{}',
            settings_snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT ''
        )
    """,
    "agent_orchestration_members": """
        CREATE TABLE IF NOT EXISTS agent_orchestration_members (
            orchestration_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            wave INTEGER NOT NULL DEFAULT 0,
            sequence INTEGER NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            retry_of_run_id TEXT NOT NULL DEFAULT '',
            dependency_run_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            PRIMARY KEY (orchestration_id, run_id)
        )
    """,
    "agent_orchestration_messages": """
        CREATE TABLE IF NOT EXISTS agent_orchestration_messages (
            id TEXT PRIMARY KEY,
            orchestration_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT ''
        )
    """,
}

_COLUMN_DEFINITIONS: dict[str, dict[str, str]] = {
    "agent_runs": {
        "id": "TEXT PRIMARY KEY",
        "kind": "TEXT NOT NULL DEFAULT 'subagent'",
        "status": "TEXT NOT NULL DEFAULT 'queued'",
        "status_message": "TEXT DEFAULT ''",
        "parent_run_id": "TEXT DEFAULT ''",
        "root_run_id": "TEXT DEFAULT ''",
        "parent_thread_id": "TEXT DEFAULT ''",
        "parent_message_id": "TEXT DEFAULT ''",
        "thread_id": "TEXT DEFAULT ''",
        "task_id": "TEXT DEFAULT ''",
        "goal_id": "TEXT DEFAULT ''",
        "depth": "INTEGER DEFAULT 0",
        "profile_id": "TEXT DEFAULT ''",
        "profile_slug": "TEXT DEFAULT ''",
        "profile_display_name": "TEXT DEFAULT ''",
        "profile_snapshot_json": "TEXT DEFAULT '{}'",
        "display_name": "TEXT DEFAULT ''",
        "prompt": "TEXT DEFAULT ''",
        "context_mode": "TEXT DEFAULT ''",
        "context_summary": "TEXT DEFAULT ''",
        "model_override": "TEXT DEFAULT ''",
        "tools_override": "TEXT DEFAULT '[]'",
        "skills_override": "TEXT DEFAULT '[]'",
        "approval_mode": "TEXT DEFAULT ''",
        "workspace_id": "TEXT DEFAULT ''",
        "workspace_path": "TEXT DEFAULT ''",
        "workspace_mode": "TEXT DEFAULT ''",
        "write_lock_key": "TEXT DEFAULT ''",
        "created_at": "TEXT NOT NULL",
        "started_at": "TEXT DEFAULT ''",
        "finished_at": "TEXT DEFAULT ''",
        "last_event_at": "TEXT DEFAULT ''",
        "timeout_at": "TEXT DEFAULT ''",
        "max_turns": "INTEGER DEFAULT 0",
        "turns_used": "INTEGER DEFAULT 0",
        "model_iterations_max": "INTEGER DEFAULT 0",
        "model_iterations_used": "INTEGER DEFAULT 0",
        "terminal_reason": "TEXT DEFAULT ''",
        "heartbeat_at": "TEXT DEFAULT ''",
        "active_seconds": "REAL DEFAULT 0",
        "token_budget": "INTEGER DEFAULT 0",
        "tokens_used": "INTEGER DEFAULT 0",
        "cost_estimate": "REAL DEFAULT 0",
        "summary": "TEXT DEFAULT ''",
        "result_json": "TEXT DEFAULT '{}'",
        "error": "TEXT DEFAULT ''",
        "settings_snapshot_json": "TEXT DEFAULT '{}'",
        "resume_state_json": "TEXT DEFAULT '{}'",
        "stop_requested": "INTEGER DEFAULT 0",
        "updated_at": "TEXT NOT NULL",
    },
    "agent_run_events": {
        "id": "TEXT PRIMARY KEY",
        "run_id": "TEXT NOT NULL",
        "ts": "TEXT NOT NULL",
        "type": "TEXT NOT NULL",
        "visibility": "TEXT DEFAULT 'log'",
        "payload_json": "TEXT DEFAULT '{}'",
    },
    "agent_run_edges": {
        "parent_run_id": "TEXT NOT NULL",
        "child_run_id": "TEXT NOT NULL",
        "relation": "TEXT DEFAULT 'delegated'",
        "created_at": "TEXT NOT NULL",
    },
    "agent_write_locks": {
        "lock_key": "TEXT PRIMARY KEY",
        "run_id": "TEXT NOT NULL",
        "parent_run_id": "TEXT DEFAULT ''",
        "thread_id": "TEXT DEFAULT ''",
        "workspace_id": "TEXT DEFAULT ''",
        "workspace_path": "TEXT DEFAULT ''",
        "acquired_at": "TEXT NOT NULL",
        "expires_at": "TEXT DEFAULT ''",
        "metadata_json": "TEXT DEFAULT '{}'",
    },
    "thread_goals": {
        "id": "TEXT PRIMARY KEY",
        "thread_id": "TEXT NOT NULL",
        "objective": "TEXT NOT NULL",
        "status": "TEXT DEFAULT 'active'",
        "created_at": "TEXT NOT NULL",
        "updated_at": "TEXT NOT NULL",
        "turns_used": "INTEGER DEFAULT 0",
        "max_turns": "INTEGER DEFAULT 20",
        "token_budget": "INTEGER DEFAULT 0",
        "tokens_used": "INTEGER DEFAULT 0",
        "last_verdict": "TEXT DEFAULT ''",
        "last_reason": "TEXT DEFAULT ''",
        "last_progress": "TEXT DEFAULT ''",
        "evidence_json": "TEXT DEFAULT '[]'",
        "subgoals_json": "TEXT DEFAULT '[]'",
        "blockers_json": "TEXT DEFAULT '[]'",
        "last_blocker": "TEXT DEFAULT ''",
        "blocker_count": "INTEGER DEFAULT 0",
        "verifier_failures": "INTEGER DEFAULT 0",
        "active_run_id": "TEXT DEFAULT ''",
        "last_turn_id": "TEXT DEFAULT ''",
        "continuation_key": "TEXT DEFAULT ''",
    },
    "agent_orchestrations": {
        "id": "TEXT PRIMARY KEY",
        "parent_thread_id": "TEXT NOT NULL DEFAULT ''",
        "parent_generation_id": "TEXT NOT NULL DEFAULT ''",
        "parent_run_id": "TEXT NOT NULL DEFAULT ''",
        "root_objective": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'planning'",
        "model_ref": "TEXT NOT NULL DEFAULT ''",
        "approval_mode": "TEXT NOT NULL DEFAULT ''",
        "runtime_surface": "TEXT NOT NULL DEFAULT 'chat'",
        "required_total": "INTEGER NOT NULL DEFAULT 0",
        "optional_total": "INTEGER NOT NULL DEFAULT 0",
        "acknowledgement_sent": "INTEGER NOT NULL DEFAULT 0",
        "continuation_state_json": "TEXT NOT NULL DEFAULT '{}'",
        "delivery_context_json": "TEXT NOT NULL DEFAULT '{}'",
        "settings_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "completed_at": "TEXT NOT NULL DEFAULT ''",
        "error_message": "TEXT NOT NULL DEFAULT ''",
    },
    "agent_orchestration_members": {
        "orchestration_id": "TEXT NOT NULL DEFAULT ''",
        "run_id": "TEXT NOT NULL DEFAULT ''",
        "required": "INTEGER NOT NULL DEFAULT 1",
        "wave": "INTEGER NOT NULL DEFAULT 0",
        "sequence": "INTEGER NOT NULL DEFAULT 0",
        "attempt": "INTEGER NOT NULL DEFAULT 1",
        "retry_of_run_id": "TEXT NOT NULL DEFAULT ''",
        "dependency_run_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "status": "TEXT NOT NULL DEFAULT 'queued'",
    },
    "agent_orchestration_messages": {
        "id": "TEXT PRIMARY KEY",
        "orchestration_id": "TEXT NOT NULL DEFAULT ''",
        "run_id": "TEXT NOT NULL DEFAULT ''",
        "kind": "TEXT NOT NULL DEFAULT ''",
        "content": "TEXT NOT NULL DEFAULT ''",
        "delivery_status": "TEXT NOT NULL DEFAULT 'pending'",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "delivered_at": "TEXT NOT NULL DEFAULT ''",
    },
}

_RUN_JSON_FIELDS = {
    "profile_snapshot_json",
    "tools_override",
    "skills_override",
    "result_json",
    "settings_snapshot_json",
    "resume_state_json",
}


class AgentRunError(ValueError):
    """Raised when an Agent Run payload or transition is invalid."""


def _now() -> str:
    return datetime.now().isoformat()


def _current_db_path() -> str:
    from row_bot import tasks

    return str(tasks._DB_PATH)


def _get_conn() -> sqlite3.Connection:
    from row_bot.tasks import _get_conn as _tasks_conn

    return _tasks_conn()


def _json_text(value: Any, *, list_ok: bool = False) -> str:
    if value is None or value == "":
        value = [] if list_ok else {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AgentRunError("Agent Run JSON fields must contain valid JSON.") from exc
        value = parsed
    if list_ok:
        if not isinstance(value, list):
            raise AgentRunError("Agent Run JSON list field must be a list.")
    elif not isinstance(value, dict):
        raise AgentRunError("Agent Run JSON object field must be an object.")
    return json.dumps(value, sort_keys=True)


def _parse_json(value: Any, *, list_ok: bool = False) -> Any:
    if value is None or value == "":
        return [] if list_ok else {}
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    try:
        parsed = json.loads(str(value))
    except Exception:
        return [] if list_ok else {}
    if list_ok:
        return parsed if isinstance(parsed, list) else []
    return parsed if isinstance(parsed, dict) else {}


def _normalize_kind(kind: str) -> str:
    value = str(kind or "subagent").strip().lower()
    if value not in AGENT_RUN_KINDS:
        raise AgentRunError(f"Invalid Agent Run kind: {kind}")
    return value


def _normalize_status(status: str) -> str:
    value = str(status or "queued").strip().lower()
    if value not in AGENT_RUN_STATUSES:
        raise AgentRunError(f"Invalid Agent Run status: {status}")
    return value


def _normalize_visibility(visibility: str) -> str:
    value = str(visibility or "log").strip().lower()
    if value not in AGENT_EVENT_VISIBILITIES:
        raise AgentRunError(f"Invalid Agent Run event visibility: {visibility}")
    return value


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _apply_schema(conn: sqlite3.Connection) -> None:
    for sql in _CREATE_TABLE_SQL.values():
        conn.execute(sql)
    for table, columns in _COLUMN_DEFINITIONS.items():
        existing = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                existing.add(column)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_thread "
        "ON agent_runs(thread_id, status, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_parent_thread "
        "ON agent_runs(parent_thread_id, status, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_root_status "
        "ON agent_runs(root_run_id, status, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_task "
        "ON agent_runs(task_id, kind, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_run_events_run_ts "
        "ON agent_run_events(run_id, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thread_goals_thread_status "
        "ON thread_goals(thread_id, status, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_orchestrations_parent "
        "ON agent_orchestrations(parent_thread_id, status, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_orchestrations_generation "
        "ON agent_orchestrations(parent_thread_id, parent_generation_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_orchestration_members_run "
        "ON agent_orchestration_members(run_id, orchestration_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_orchestration_messages_delivery "
        "ON agent_orchestration_messages(orchestration_id, delivery_status, created_at)"
    )


def ensure_agent_run_schema(*, force: bool = False) -> None:
    """Create and migrate Agent Run tables in tasks.db."""
    global _SCHEMA_READY_PATH
    db_path = _current_db_path()
    with _SCHEMA_LOCK:
        if _SCHEMA_READY_PATH == db_path and not force:
            return
        from row_bot.agent_profiles import ensure_agent_profiles_schema

        ensure_agent_profiles_schema(force=force)
        conn = _get_conn()
        try:
            _apply_schema(conn)
            conn.commit()
            _SCHEMA_READY_PATH = db_path
        finally:
            conn.close()


def _profile_snapshot(
    profile_id_or_slug: str,
    *,
    parent_approval_mode: str = "",
) -> tuple[str, str, str, dict[str, Any]]:
    ref = str(profile_id_or_slug or "").strip()
    if not ref:
        return "", "", "", {}
    from row_bot.agent_profiles import mark_agent_profile_used, resolve_profile_for_run

    resolved = resolve_profile_for_run(
        ref,
        parent_approval_mode=parent_approval_mode or None,
    )
    snapshot = resolved["profile_snapshot"]
    mark_agent_profile_used(snapshot["id"])
    return (
        str(snapshot.get("id") or ""),
        str(snapshot.get("slug") or ""),
        str(snapshot.get("display_name") or ""),
        snapshot,
    )


def _run_from_row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for field in _RUN_JSON_FIELDS:
        data[field] = _parse_json(
            data.get(field),
            list_ok=field in {"tools_override", "skills_override"},
        )
    for field in (
        "depth",
        "max_turns",
        "turns_used",
        "model_iterations_max",
        "model_iterations_used",
        "token_budget",
        "tokens_used",
    ):
        data[field] = _int_value(data.get(field))
    data["cost_estimate"] = _float_value(data.get("cost_estimate"))
    data["active_seconds"] = _float_value(data.get("active_seconds"))
    data["stop_requested"] = bool(data.get("stop_requested", 0))
    return data


def _event_from_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["payload_json"] = _parse_json(data.get("payload_json"))
    return data


def get_agent_settings_snapshot(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from row_bot.agent_settings import load_agent_runtime_settings

    runtime = load_agent_runtime_settings()
    snapshot = dict(DEFAULT_AGENT_SETTINGS)
    snapshot.update(runtime.to_dict())
    for key, value in dict(overrides or {}).items():
        if key in {"default_context_mode", "default_workspace_mode", "goal_max_turns"}:
            snapshot[key] = value
    return snapshot


def create_agent_run(
    *,
    run_id: str | None = None,
    kind: str = "subagent",
    status: str = "queued",
    status_message: str = "",
    parent_run_id: str = "",
    root_run_id: str = "",
    parent_thread_id: str = "",
    parent_message_id: str = "",
    thread_id: str = "",
    task_id: str = "",
    goal_id: str = "",
    depth: int = 0,
    profile_id: str = "",
    profile_snapshot_json: Mapping[str, Any] | None = None,
    display_name: str = "",
    prompt: str = "",
    context_mode: str = "",
    context_summary: str = "",
    model_override: str = "",
    tools_override: Sequence[str] | str | None = None,
    skills_override: Sequence[str] | str | None = None,
    approval_mode: str = "",
    workspace_id: str = "",
    workspace_path: str = "",
    workspace_mode: str = "",
    write_lock_key: str = "",
    timeout_at: str = "",
    max_turns: int = 0,
    turns_used: int = 0,
    model_iterations_max: int = 0,
    model_iterations_used: int = 0,
    terminal_reason: str = "",
    heartbeat_at: str = "",
    active_seconds: float = 0.0,
    token_budget: int = 0,
    tokens_used: int = 0,
    cost_estimate: float = 0.0,
    summary: str = "",
    result_json: Mapping[str, Any] | None = None,
    error: str = "",
    settings_snapshot_json: Mapping[str, Any] | None = None,
    resume_state_json: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update an Agent Run row and snapshot its effective profile."""
    ensure_agent_run_schema()
    run_id = str(run_id or uuid.uuid4().hex[:12])
    kind = _normalize_kind(kind)
    status = _normalize_status(status)
    now = _now()
    started_at = now if status == "running" else ""
    finished_at = now if status in TERMINAL_STATUSES else ""
    profile_snapshot = dict(profile_snapshot_json or {})
    profile_ref = profile_id or str(profile_snapshot.get("id") or "")
    if profile_ref and not profile_snapshot:
        (
            profile_id,
            profile_slug,
            profile_display_name,
            profile_snapshot,
        ) = _profile_snapshot(profile_ref, parent_approval_mode=approval_mode)
    elif profile_snapshot:
        profile_id = str(profile_snapshot.get("id") or profile_ref)
        profile_slug = str(profile_snapshot.get("slug") or "")
        profile_display_name = str(profile_snapshot.get("display_name") or "")
    else:
        profile_id = profile_slug = profile_display_name = ""
    if not context_mode and profile_snapshot:
        context_policy = profile_snapshot.get("context_policy_json") or {}
        if isinstance(context_policy, dict):
            context_mode = str(context_policy.get("default_context_mode") or "")
    settings_snapshot = get_agent_settings_snapshot(settings_snapshot_json)
    root_run_id = str(root_run_id or "").strip() or run_id
    if not model_iterations_max:
        model_iterations_max = _int_value(settings_snapshot.get("max_iterations"))

    values = {
        "id": run_id,
        "kind": kind,
        "status": status,
        "status_message": str(status_message or ""),
        "parent_run_id": str(parent_run_id or ""),
        "root_run_id": root_run_id,
        "parent_thread_id": str(parent_thread_id or ""),
        "parent_message_id": str(parent_message_id or ""),
        "thread_id": str(thread_id or ""),
        "task_id": str(task_id or ""),
        "goal_id": str(goal_id or ""),
        "depth": _int_value(depth),
        "profile_id": profile_id,
        "profile_slug": profile_slug,
        "profile_display_name": profile_display_name,
        "profile_snapshot_json": _json_text(profile_snapshot),
        "display_name": str(display_name or ""),
        "prompt": str(prompt or ""),
        "context_mode": str(context_mode or ""),
        "context_summary": str(context_summary or ""),
        "model_override": str(model_override or ""),
        "tools_override": _json_text(tools_override, list_ok=True),
        "skills_override": _json_text(skills_override, list_ok=True),
        "approval_mode": str(approval_mode or ""),
        "workspace_id": str(workspace_id or ""),
        "workspace_path": str(workspace_path or ""),
        "workspace_mode": str(workspace_mode or ""),
        "write_lock_key": str(write_lock_key or ""),
        "started_at": started_at,
        "finished_at": finished_at,
        "last_event_at": "",
        "timeout_at": str(timeout_at or ""),
        "max_turns": _int_value(max_turns),
        "turns_used": _int_value(turns_used),
        "model_iterations_max": _int_value(model_iterations_max),
        "model_iterations_used": _int_value(model_iterations_used),
        "terminal_reason": str(terminal_reason or ""),
        "heartbeat_at": str(heartbeat_at or (now if status == "running" else "")),
        "active_seconds": _float_value(active_seconds),
        "token_budget": _int_value(token_budget),
        "tokens_used": _int_value(tokens_used),
        "cost_estimate": _float_value(cost_estimate),
        "summary": str(summary or ""),
        "result_json": _json_text(result_json),
        "error": str(error or ""),
        "settings_snapshot_json": _json_text(settings_snapshot),
        "resume_state_json": _json_text(resume_state_json),
        "stop_requested": 0,
        "updated_at": now,
    }

    conn = _get_conn()
    try:
        existing = conn.execute(
            "SELECT created_at, started_at, finished_at FROM agent_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        values["created_at"] = existing["created_at"] if existing else now
        if existing:
            if existing["started_at"] and not values["started_at"]:
                values["started_at"] = existing["started_at"]
            if existing["finished_at"] and not values["finished_at"]:
                values["finished_at"] = existing["finished_at"]
            assignments = ", ".join(f"{key} = ?" for key in values)
            params = [values[key] for key in values] + [run_id]
            conn.execute(f"UPDATE agent_runs SET {assignments} WHERE id = ?", params)
        else:
            columns = ["id", *values.keys()]
            params = [run_id, *values.values()]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO agent_runs ({', '.join(columns)}) VALUES ({placeholders})",
                params,
            )
        conn.commit()
    finally:
        conn.close()

    append_agent_event(
        run_id,
        "run.started" if status == "running" else "run.created",
        {"kind": kind, "status": status, "display_name": display_name},
        visibility="internal",
    )
    run = get_agent_run(run_id)
    assert run is not None
    return run


def start_agent_run(run_id: str) -> dict[str, Any] | None:
    """Mark a queued Agent Run as running."""
    ensure_agent_run_schema()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'running', started_at = COALESCE(NULLIF(started_at, ''), ?), "
            "heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (now, now, now, run_id),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    if not changed:
        return None
    append_agent_event(run_id, "run.started", {}, visibility="internal")
    try:
        from row_bot.agent_orchestrator import handle_run_status

        handle_run_status(run_id, "running")
    except Exception:
        pass
    return get_agent_run(run_id)


def _elapsed_active_seconds(previous: str, now: str) -> float:
    try:
        before = datetime.fromisoformat(str(previous or ""))
        after = datetime.fromisoformat(str(now or ""))
        return max(0.0, (after - before).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def update_agent_budget_progress(
    run_id: str,
    *,
    used_iterations: int,
    max_iterations: int,
) -> dict[str, Any] | None:
    """Persist safe budget usage and heartbeat without storing model inputs."""

    ensure_agent_run_schema()
    now = _now()
    conn = _get_conn()
    try:
        current = conn.execute(
            "SELECT status, heartbeat_at, active_seconds FROM agent_runs WHERE id = ?",
            (str(run_id),),
        ).fetchone()
        if current is None:
            return None
        active = _float_value(current["active_seconds"])
        if str(current["status"] or "") == "running":
            active += _elapsed_active_seconds(str(current["heartbeat_at"] or ""), now)
        conn.execute(
            "UPDATE agent_runs SET model_iterations_used = ?, model_iterations_max = ?, "
            "heartbeat_at = ?, active_seconds = ?, updated_at = ? WHERE id = ?",
            (
                max(0, _int_value(used_iterations)),
                max(0, _int_value(max_iterations)),
                now,
                active,
                now,
                str(run_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_agent_run(run_id)


def heartbeat_agent_run(run_id: str) -> dict[str, Any] | None:
    run = get_agent_run(run_id)
    if not run:
        return None
    return update_agent_budget_progress(
        run_id,
        used_iterations=_int_value(run.get("model_iterations_used")),
        max_iterations=_int_value(run.get("model_iterations_max")),
    )


def append_agent_event(
    run_id: str,
    type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    visibility: str = "log",
) -> dict[str, Any]:
    """Append a timestamped event to an Agent Run."""
    ensure_agent_run_schema()
    event_id = uuid.uuid4().hex[:12]
    ts = _now()
    visibility = _normalize_visibility(visibility)
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_run_events "
            "(id, run_id, ts, type, visibility, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_id,
                str(run_id),
                ts,
                str(type or "run.status"),
                visibility,
                _json_text(payload),
            ),
        )
        conn.execute(
            "UPDATE agent_runs SET last_event_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, str(run_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": event_id,
        "run_id": str(run_id),
        "ts": ts,
        "type": str(type or "run.status"),
        "visibility": visibility,
        "payload_json": dict(payload or {}),
    }


def update_agent_status(
    run_id: str,
    status: str,
    status_message: str = "",
    *,
    append_event: bool = True,
) -> dict[str, Any] | None:
    """Update an Agent Run status without marking it finished."""
    ensure_agent_run_schema()
    status = _normalize_status(status)
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = ?, status_message = ?, "
            "updated_at = ? WHERE id = ?",
            (status, str(status_message or ""), now, str(run_id)),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    if not changed:
        return None
    if append_event:
        append_agent_event(
            run_id,
            "run.status",
            {"status": status, "status_message": status_message},
            visibility="internal",
        )
    try:
        from row_bot.agent_orchestrator import handle_run_status

        handle_run_status(run_id, status)
    except Exception:
        pass
    return get_agent_run(run_id)


def save_agent_resume_state(
    run_id: str,
    resume_state_json: Mapping[str, Any],
    *,
    status: str = "waiting_approval",
    status_message: str = "",
) -> dict[str, Any] | None:
    """Persist resumable state for an interrupted Agent Run."""
    ensure_agent_run_schema()
    status = _normalize_status(status)
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = ?, status_message = ?, "
            "resume_state_json = ?, updated_at = ? WHERE id = ?",
            (
                status,
                str(status_message or ""),
                _json_text(resume_state_json),
                now,
                str(run_id),
            ),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    if not changed:
        return None
    append_agent_event(
        run_id,
        "approval.requested" if status == "waiting_approval" else "run.status",
        {"status": status, "status_message": status_message},
        visibility="log",
    )
    try:
        from row_bot.agent_orchestrator import handle_run_status

        handle_run_status(run_id, status)
    except Exception:
        pass
    return get_agent_run(run_id)


def finish_agent_run(
    run_id: str,
    status: str,
    summary: str = "",
    result_json: Mapping[str, Any] | None = None,
    error: str = "",
    status_message: str = "",
    terminal_reason: str = "",
) -> dict[str, Any] | None:
    """Mark an Agent Run as terminal and persist its final payload."""
    ensure_agent_run_schema()
    status = _normalize_status(status)
    if status not in TERMINAL_STATUSES:
        return update_agent_status(run_id, status, status_message)
    now = _now()
    conn = _get_conn()
    preserve_stopped = False
    try:
        current = conn.execute(
            "SELECT status, stop_requested, heartbeat_at, active_seconds FROM agent_runs WHERE id = ?",
            (str(run_id),),
        ).fetchone()
        if (
            current
            and str(current["status"] or "") == "stopped"
            and int(current["stop_requested"] or 0)
            and status not in {"stopped", "cancelled"}
        ):
            preserve_stopped = True
            changed = 0
        else:
            active_seconds = _float_value(current["active_seconds"] if current else 0)
            if current and str(current["status"] or "") == "running":
                active_seconds += _elapsed_active_seconds(str(current["heartbeat_at"] or ""), now)
            conn.execute(
                "UPDATE agent_runs SET status = ?, status_message = ?, "
                "summary = ?, result_json = ?, error = ?, terminal_reason = ?, "
                "heartbeat_at = ?, active_seconds = ?, finished_at = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    status,
                    str(status_message or error or summary or ""),
                    str(summary or ""),
                    _json_text(result_json),
                    str(error or ""),
                    str(terminal_reason or ""),
                    now,
                    active_seconds,
                    now,
                    now,
                    str(run_id),
                ),
            )
            changed = conn.total_changes
            conn.commit()
    finally:
        conn.close()
    if preserve_stopped:
        return get_agent_run(run_id)
    if not changed:
        return None
    event_type = {
        "completed": "run.completed",
        "completed_delivery_failed": "run.completed",
        "failed": "run.failed",
        "stopped": "run.stopped",
        "blocked": "run.blocked",
        "timed_out": "run.failed",
        "cancelled": "run.stopped",
    }.get(status, "run.status")
    append_agent_event(
        run_id,
        event_type,
        {
            "status": status,
            "status_message": status_message,
            "summary": summary,
            "error": error,
            "terminal_reason": terminal_reason,
        },
        visibility="log",
    )
    final_run = get_agent_run(run_id)
    orchestration_owned = False
    try:
        from row_bot.agent_orchestrator import handle_run_terminal

        orchestration_owned = handle_run_terminal(final_run or run_id)
    except Exception:
        # A child result remains durable even if orchestration reconciliation
        # needs startup repair or an explicit Resume.
        orchestration_owned = False
    try:
        run_kind = str((final_run or {}).get("kind") or "").lower()
        if run_kind != "goal" and not orchestration_owned:
            from row_bot.channels.thread_notifications import notify_agent_run_terminal

            notify_agent_run_terminal(final_run or run_id)
    except Exception:
        pass
    return final_run


def record_agent_run_progress(
    run_id: str,
    *,
    steps_done: int,
    steps_total: int = 0,
    label: str = "",
) -> dict[str, Any] | None:
    """Mirror workflow step progress into the Agent Run row and event log."""
    ensure_agent_run_schema()
    steps_done = max(0, _int_value(steps_done))
    steps_total = max(0, _int_value(steps_total))
    now = _now()
    status_message = (
        f"Step {steps_done}/{steps_total}" if steps_total else f"Step {steps_done}"
    )
    if label:
        status_message += f": {label}"
    conn = _get_conn()
    try:
        if steps_total:
            conn.execute(
                "UPDATE agent_runs SET turns_used = ?, max_turns = ?, "
                "status_message = ?, updated_at = ? WHERE id = ?",
                (steps_done, steps_total, status_message, now, str(run_id)),
            )
        else:
            conn.execute(
                "UPDATE agent_runs SET turns_used = ?, status_message = ?, "
                "updated_at = ? WHERE id = ?",
                (steps_done, status_message, now, str(run_id)),
            )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    if not changed:
        return None
    append_agent_event(
        run_id,
        "turn.completed",
        {"steps_done": steps_done, "steps_total": steps_total, "label": label},
        visibility="log",
    )
    return get_agent_run(run_id)


def create_agent_run_edge(
    parent_run_id: str,
    child_run_id: str,
    relation: str = "delegated",
) -> dict[str, str]:
    ensure_agent_run_schema()
    created_at = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO agent_run_edges "
            "(parent_run_id, child_run_id, relation, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(parent_run_id), str(child_run_id), str(relation or "delegated"), created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "parent_run_id": str(parent_run_id),
        "child_run_id": str(child_run_id),
        "relation": str(relation or "delegated"),
        "created_at": created_at,
    }


def get_agent_run(run_id: str) -> dict[str, Any] | None:
    ensure_agent_run_schema()
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (str(run_id),)).fetchone()
    finally:
        conn.close()
    return _run_from_row(row)


def get_agent_run_for_thread(thread_id: str) -> dict[str, Any] | None:
    """Return the child Agent run that owns a child thread, if any."""
    ensure_agent_run_schema()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE thread_id = ? AND kind = 'subagent' "
            "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (str(thread_id),),
        ).fetchone()
    finally:
        conn.close()
    return _run_from_row(row)


def get_agent_events(run_id: str, limit: int = 100) -> list[dict[str, Any]]:
    ensure_agent_run_schema()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_run_events WHERE run_id = ? "
            "ORDER BY ts ASC LIMIT ?",
            (str(run_id), max(1, _int_value(limit, 100))),
        ).fetchall()
    finally:
        conn.close()
    return [_event_from_row(row) for row in rows]


def append_agent_parent_message(run_id: str, message: str) -> dict[str, Any] | None:
    """Record a parent steering/follow-up message for a child Agent Run."""
    ensure_agent_run_schema()
    run = get_agent_run(run_id)
    if not run:
        return None
    text = str(message or "").strip()
    if not text:
        raise AgentRunError("Parent Agent message cannot be empty.")
    append_agent_event(
        run_id,
        "parent.message",
        {"message": text},
        visibility="user_visible",
    )
    if str(run.get("status") or "") == "queued":
        update_agent_status(run_id, "queued", "Parent message queued")
    return get_agent_run(run_id)


def get_agent_parent_messages(run_id: str, limit: int = 20) -> list[str]:
    """Return the newest parent steering messages in chronological order."""

    ensure_agent_run_schema()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT payload_json FROM agent_run_events "
            "WHERE run_id = ? AND type = 'parent.message' "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (str(run_id), max(1, int(limit or 20))),
        ).fetchall()
    finally:
        conn.close()
    messages: list[str] = []
    for row in reversed(rows):
        payload = _parse_json(row["payload_json"])
        if isinstance(payload, dict):
            text = str(payload.get("message") or "").strip()
            if text:
                messages.append(text)
    return messages


def _status_filter(statuses: str | Iterable[str] | None) -> list[str]:
    if statuses is None:
        return []
    if isinstance(statuses, str):
        return [_normalize_status(statuses)]
    return [_normalize_status(status) for status in statuses]


def list_agent_runs(
    *,
    parent_thread_id: str | None = None,
    statuses: str | Iterable[str] | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_agent_run_schema()
    clauses: list[str] = []
    params: list[Any] = []
    if parent_thread_id:
        clauses.append("parent_thread_id = ?")
        params.append(str(parent_thread_id))
    status_values = _status_filter(statuses)
    if status_values:
        clauses.append(f"status IN ({', '.join('?' for _ in status_values)})")
        params.extend(status_values)
    if kind:
        clauses.append("kind = ?")
        params.append(_normalize_kind(kind))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, _int_value(limit, 50)))
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_runs "
            f"{where} "
            "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [run for row in rows if (run := _run_from_row(row)) is not None]


def list_child_runs(
    *,
    parent_thread_id: str | None = None,
    parent_run_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_agent_run_schema()
    clauses: list[str] = []
    params: list[Any] = []
    if parent_thread_id:
        clauses.append("r.parent_thread_id = ?")
        params.append(str(parent_thread_id))
    if parent_run_id:
        clauses.append("e.parent_run_id = ?")
        params.append(str(parent_run_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT r.*, e.parent_run_id AS edge_parent_run_id, "
            "e.relation AS edge_relation, e.created_at AS edge_created_at "
            "FROM agent_runs r JOIN agent_run_edges e ON r.id = e.child_run_id "
            f"{where} "
            "ORDER BY r.updated_at DESC, r.created_at DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    runs: list[dict[str, Any]] = []
    for row in rows:
        run = _run_from_row(row)
        if run is None:
            continue
        run["edge_parent_run_id"] = row["edge_parent_run_id"]
        run["edge_relation"] = row["edge_relation"]
        run["edge_created_at"] = row["edge_created_at"]
        runs.append(run)
    return runs


def stop_agent_run(run_id: str) -> dict[str, Any] | None:
    """Request stop and mark the run stopped until a live runner can exit."""
    ensure_agent_run_schema()
    run = get_agent_run(run_id)
    if not run:
        return None
    now = _now()
    terminal = str(run.get("status") or "") in TERMINAL_STATUSES
    conn = _get_conn()
    try:
        if terminal:
            conn.execute(
                "UPDATE agent_runs SET stop_requested = 1, updated_at = ? WHERE id = ?",
                (now, str(run_id)),
            )
        else:
            conn.execute(
                "UPDATE agent_runs SET stop_requested = 1, status = 'stopped', "
                "status_message = 'Stop requested', finished_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, str(run_id)),
            )
        conn.commit()
    finally:
        conn.close()
    append_agent_event(run_id, "run.stopped", {"requested": True}, visibility="log")
    return get_agent_run(run_id)


def acquire_agent_write_lock(
    lock_key: str,
    run_id: str,
    *,
    parent_run_id: str = "",
    thread_id: str = "",
    workspace_id: str = "",
    workspace_path: str = "",
    metadata_json: Mapping[str, Any] | None = None,
) -> bool:
    """Try to acquire a single-writer lock. Returns False if already held."""
    ensure_agent_run_schema()
    key = str(lock_key or "").strip()
    if not key:
        raise AgentRunError("Agent write lock key cannot be empty.")
    now = _now()
    conn = _get_conn()
    try:
        try:
            conn.execute(
                "INSERT INTO agent_write_locks "
                "(lock_key, run_id, parent_run_id, thread_id, workspace_id, "
                "workspace_path, acquired_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    str(run_id),
                    str(parent_run_id or ""),
                    str(thread_id or ""),
                    str(workspace_id or ""),
                    str(workspace_path or ""),
                    now,
                    _json_text(metadata_json),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        conn.commit()
    finally:
        conn.close()
    append_agent_event(
        run_id,
        "write_lock.acquired",
        {"lock_key": key},
        visibility="internal",
    )
    return True


def release_agent_write_lock(
    lock_key: str = "",
    *,
    run_id: str = "",
) -> bool:
    """Release a writer lock by key or owning run id."""
    ensure_agent_run_schema()
    if not lock_key and not run_id:
        raise AgentRunError("Provide lock_key or run_id to release a write lock.")
    conn = _get_conn()
    released_rows: list[sqlite3.Row]
    try:
        if lock_key:
            released_rows = conn.execute(
                "SELECT * FROM agent_write_locks WHERE lock_key = ?",
                (str(lock_key),),
            ).fetchall()
            conn.execute("DELETE FROM agent_write_locks WHERE lock_key = ?", (str(lock_key),))
        else:
            released_rows = conn.execute(
                "SELECT * FROM agent_write_locks WHERE run_id = ?",
                (str(run_id),),
            ).fetchall()
            conn.execute("DELETE FROM agent_write_locks WHERE run_id = ?", (str(run_id),))
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    for row in released_rows:
        append_agent_event(
            str(row["run_id"]),
            "write_lock.released",
            {"lock_key": row["lock_key"]},
            visibility="internal",
        )
    return bool(changed)


def get_agent_write_lock(lock_key: str) -> dict[str, Any] | None:
    ensure_agent_run_schema()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_write_locks WHERE lock_key = ?",
            (str(lock_key),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    data = dict(row)
    data["metadata_json"] = _parse_json(data.get("metadata_json"))
    return data


def list_agent_write_locks() -> list[dict[str, Any]]:
    ensure_agent_run_schema()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_write_locks ORDER BY acquired_at ASC"
        ).fetchall()
    finally:
        conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["metadata_json"] = _parse_json(data.get("metadata_json"))
        result.append(data)
    return result


def _sql_placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _timeout_expired(timeout_at: str) -> bool:
    raw = str(timeout_at or "").strip()
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) <= datetime.now()
    except ValueError:
        return False


def _thread_row_exists(thread_id: str) -> bool:
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return False
    try:
        from row_bot.threads import _thread_exists

        return bool(_thread_exists(thread_id))
    except Exception:
        return False


def recover_stale_agent_runs() -> dict[str, int]:
    """Normalize durable Agent Run rows after app/process restart.

    In-process worker threads do not survive a restart, so running rows and
    dead writer locks need a durable recovery pass. Resumable approval rows are
    preserved, while non-resumable active rows are stopped.
    """

    ensure_agent_run_schema()
    orchestration_recovery = {
        "orchestrations_interrupted": 0,
        "members_interrupted": 0,
    }
    try:
        from row_bot.agent_orchestrator import recover_interrupted_orchestrations

        orchestration_recovery = recover_interrupted_orchestrations()
    except Exception:
        # Legacy recovery remains available even when additive orchestration
        # repair encounters an unexpected row.
        pass
    now = _now()
    stopped: list[tuple[str, str]] = []
    kept_approvals: list[str] = []
    released_locks: list[tuple[str, str]] = []
    conn = _get_conn()
    try:
        lock_rows = conn.execute("SELECT * FROM agent_write_locks").fetchall()
        released_locks = [
            (str(row["run_id"] or ""), str(row["lock_key"] or ""))
            for row in lock_rows
        ]
        conn.execute("DELETE FROM agent_write_locks")

        placeholders = _sql_placeholders(tuple(TERMINAL_STATUSES))
        rows = conn.execute(
            f"SELECT * FROM agent_runs WHERE status NOT IN ({placeholders})",
            tuple(TERMINAL_STATUSES),
        ).fetchall()
        orchestration_run_ids = {
            str(row["run_id"])
            for row in conn.execute(
                "SELECT run_id FROM agent_orchestration_members"
            ).fetchall()
        }
        for row in rows:
            run = _run_from_row(row) or {}
            run_id = str(run.get("id") or "")
            status = str(run.get("status") or "")
            if run_id in orchestration_run_ids:
                # Durable orchestrations are explicitly resumed by the user.
                # Their interrupted rows must not be converted to legacy stops.
                continue
            if status == "waiting_approval":
                if run.get("resume_state_json"):
                    kept_approvals.append(run_id)
                    continue
                stopped.append((run_id, "App restarted before completion"))
                continue
            if status == "queued":
                stopped.append((run_id, "App restarted before queued child capacity could be restored"))
                continue
            if status == "paused":
                continue
            stopped.append((run_id, "App restarted before completion"))

        for run_id, reason in stopped:
            conn.execute(
                "UPDATE agent_runs SET stop_requested = 1, status = 'stopped', "
                "status_message = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (reason, now, now, run_id),
            )
        conn.commit()
    finally:
        conn.close()

    for run_id, lock_key in released_locks:
        if run_id:
            append_agent_event(
                run_id,
                "write_lock.released",
                {"lock_key": lock_key, "reason": "startup_recovery"},
                visibility="internal",
            )
    for run_id, reason in stopped:
        append_agent_event(
            run_id,
            "run.stopped",
            {"reason": reason, "source": "startup_recovery"},
            visibility="log",
        )
    return {
        "stopped": len(stopped),
        "queued": 0,
        "waiting_approval": len(kept_approvals),
        "locks_released": len(released_locks),
        **orchestration_recovery,
    }


def cleanup_thread_agent_runs(thread_id: str) -> dict[str, int]:
    """Delete chat-created subagent/goal run state for a parent thread.

    Workflow mirrors are intentionally preserved as task audit history.
    """

    ensure_agent_run_schema()
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return {"runs_deleted": 0, "threads_deleted": 0, "approvals_cancelled": 0, "locks_released": 0}
    now = _now()
    conn = _get_conn()
    try:
        orchestration_rows = conn.execute(
            "SELECT id FROM agent_orchestrations WHERE parent_thread_id = ?",
            (thread_id,),
        ).fetchall()
        orchestration_ids = [
            str(row["id"]) for row in orchestration_rows if str(row["id"] or "")
        ]
        if orchestration_ids:
            orchestration_placeholders = _sql_placeholders(orchestration_ids)
            conn.execute(
                f"DELETE FROM agent_orchestration_messages "
                f"WHERE orchestration_id IN ({orchestration_placeholders})",
                orchestration_ids,
            )
            conn.execute(
                f"DELETE FROM agent_orchestration_members "
                f"WHERE orchestration_id IN ({orchestration_placeholders})",
                orchestration_ids,
            )
            conn.execute(
                f"DELETE FROM agent_orchestrations "
                f"WHERE id IN ({orchestration_placeholders})",
                orchestration_ids,
            )
        rows = conn.execute(
            "SELECT * FROM agent_runs WHERE "
            "(kind = 'subagent' AND parent_thread_id = ?) "
            "OR (kind = 'goal' AND thread_id = ?)",
            (thread_id, thread_id),
        ).fetchall()
        runs = [_run_from_row(row) for row in rows]
        runs = [run for run in runs if run]
        run_ids = [str(run.get("id") or "") for run in runs if run.get("id")]
        child_thread_ids = sorted({
            str(run.get("thread_id") or "")
            for run in runs
            if str(run.get("thread_id") or "") and str(run.get("thread_id") or "") != thread_id
        })
        locks_released = 0
        approvals_cancelled = 0
        if run_ids:
            placeholders = _sql_placeholders(run_ids)
            locks_released += conn.execute(
                f"DELETE FROM agent_write_locks WHERE run_id IN ({placeholders})",
                run_ids,
            ).rowcount
            approvals_cancelled += conn.execute(
                f"UPDATE approval_requests SET status = 'cancelled', responded_at = ? "
                f"WHERE status = 'pending' AND agent_run_id IN ({placeholders})",
                [now, *run_ids],
            ).rowcount
            conn.execute(
                f"DELETE FROM agent_run_events WHERE run_id IN ({placeholders})",
                run_ids,
            )
            conn.execute(
                f"DELETE FROM agent_run_edges WHERE parent_run_id IN ({placeholders}) "
                f"OR child_run_id IN ({placeholders})",
                [*run_ids, *run_ids],
            )
            conn.execute(
                f"DELETE FROM thread_goals WHERE active_run_id IN ({placeholders})",
                run_ids,
            )
            conn.execute(
                f"DELETE FROM agent_runs WHERE id IN ({placeholders})",
                run_ids,
            )
        conn.execute("DELETE FROM thread_goals WHERE thread_id = ?", (thread_id,))
        conn.commit()
    finally:
        conn.close()

    threads_deleted = 0
    for child_thread_id in child_thread_ids:
        try:
            from row_bot.threads import _delete_thread

            _delete_thread(child_thread_id)
            threads_deleted += 1
        except Exception:
            pass
    return {
        "runs_deleted": len(run_ids),
        "threads_deleted": threads_deleted,
        "approvals_cancelled": approvals_cancelled,
        "locks_released": locks_released,
    }


def mirror_workflow_run_start(
    run_id: str,
    *,
    task_id: str,
    thread_id: str,
    display_name: str,
    steps_total: int = 0,
    profile_id: str = "",
    profile_snapshot_json: Mapping[str, Any] | None = None,
    approval_mode: str = "",
    model_override: str = "",
    tools_override: Sequence[str] | str | None = None,
    skills_override: Sequence[str] | str | None = None,
) -> dict[str, Any]:
    """Create/update the Agent Run mirror for a legacy workflow run."""
    return create_agent_run(
        run_id=run_id,
        kind="workflow",
        status="running",
        task_id=task_id,
        thread_id=thread_id,
        display_name=display_name,
        profile_id=profile_id,
        profile_snapshot_json=profile_snapshot_json,
        approval_mode=approval_mode,
        model_override=model_override,
        tools_override=tools_override,
        skills_override=skills_override,
        max_turns=steps_total,
        settings_snapshot_json=DEFAULT_AGENT_SETTINGS,
    )


def mirror_workflow_progress(
    run_id: str,
    steps_done: int,
    *,
    steps_total: int = 0,
    label: str = "",
) -> dict[str, Any] | None:
    return record_agent_run_progress(
        run_id,
        steps_done=steps_done,
        steps_total=steps_total,
        label=label,
    )


def mirror_workflow_finish(
    run_id: str,
    status: str,
    status_message: str = "",
    terminal_reason: str = "",
) -> dict[str, Any] | None:
    if not get_agent_run(run_id):
        return None
    status = _normalize_status(status)
    if status in TERMINAL_STATUSES:
        return finish_agent_run(
            run_id,
            status,
            summary=status_message if status.startswith("completed") else "",
            error=status_message if status in {"failed", "blocked", "timed_out"} else "",
            status_message=status_message,
            terminal_reason=terminal_reason,
        )
    return update_agent_status(run_id, status, status_message)
