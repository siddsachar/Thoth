"""Durable orchestration for parent turns that delegate required child work.

The UI may observe these records, but terminal child events and SQLite
transitions coordinate retries, completion barriers, synthesis, and delivery.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ORCHESTRATION_STATUSES = {
    "planning",
    "running",
    "waiting_children",
    "waiting_approval",
    "synthesizing",
    "completed",
    "completed_partial",
    "failed",
    "stopped",
    "interrupted",
}
ACTIVE_ORCHESTRATION_STATUSES = {
    "planning",
    "running",
    "waiting_children",
    "waiting_approval",
    "synthesizing",
}
ORCHESTRATION_STATUS_LABELS = {
    "planning": "Planning",
    "running": "Running",
    "waiting_children": "Waiting for Agents",
    "waiting_approval": "Needs approval",
    "synthesizing": "Preparing final answer",
    "completed": "Completed",
    "completed_partial": "Completed with issues",
    "interrupted": "Interrupted",
    "failed": "Failed",
    "stopped": "Stopped",
}
AGENT_MEMBER_STATUS_LABELS = {
    "active": "Running",
    "queued": "Queued",
    "running": "Running",
    "waiting_approval": "Needs approval",
    "waiting_user": "Needs attention",
    "paused": "Paused",
    "completed": "Completed",
    "completed_delivery_failed": "Completed; delivery failed",
    "failed": "Failed",
    "blocked": "Blocked",
    "stopped": "Stopped",
    "cleared": "Cleared",
    "cancelled": "Cancelled",
    "timed_out": "Timed out",
    "interrupted": "Interrupted",
    "retrying": "Retrying",
    "retried": "Replaced",
}
TERMINAL_MEMBER_STATUSES = {
    "completed",
    "completed_delivery_failed",
    "failed",
    "stopped",
    "blocked",
    "timed_out",
    "cancelled",
    "retried",
}
FAILED_MEMBER_STATUSES = {
    "failed",
    "stopped",
    "blocked",
    "timed_out",
    "cancelled",
}
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "temporary unavailable",
    "service unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "network transport",
    "retryable network",
    "dispatcher interruption",
    "workspace lock contention",
    "rate limit",
    "too many requests",
    "try again",
    "503",
    "504",
)
_PERMANENT_MARKERS = (
    "approval denied",
    "policy",
    "invalid argument",
    "invalid tool",
    "schema",
    "parser",
    "authentication",
    "credential",
    "api key",
    "billing",
    "safety",
    "configured model",
    "model unavailable",
    "missing model",
    "stop requested",
    "explicit stop",
)
_SERVICE_LOCK = threading.RLock()
_DELIVERY_LOCK = threading.RLock()
_SYNTHESIS_THREADS: dict[str, threading.Thread] = {}
_PARENT_THREADS: dict[str, threading.Thread] = {}
_SYNTHESIS_EXECUTOR: Callable[[dict[str, Any], str], str] | None = None
_PARENT_EXECUTOR: Callable[
    [dict[str, Any], str, list[str], dict[str, Any]], str | dict[str, Any]
] | None = None
_RETRY_EXECUTOR: Callable[[dict[str, Any], dict[str, Any], bool], dict[str, Any]] | None = None
_DELIVERY_EXECUTOR: Callable[[dict[str, Any], str, str, str], bool] | None = None
CURRENT_ORCHESTRATION_VERSION = 2
THREAD_EVENT_KINDS = {
    "child_terminal",
    "child_approval_requested",
    "child_approval_resolved",
    "child_retry_scheduled",
    "parent_steering",
    "stop_requested",
    "goal_continuation",
    "workflow_continuation",
}
CHILD_LIFECYCLE_EVENT_KINDS = {
    "child_terminal",
    "child_approval_requested",
    "child_approval_resolved",
    "child_retry_scheduled",
}
_LEGAL_TRANSITIONS = {
    "planning": {"running", "failed", "stopped", "interrupted"},
    "running": {
        "waiting_children",
        "waiting_approval",
        "failed",
        "stopped",
        "interrupted",
    },
    "waiting_children": {
        "synthesizing",
        "waiting_approval",
        "failed",
        "stopped",
        "interrupted",
    },
    "waiting_approval": {
        "waiting_children",
        "failed",
        "stopped",
        "interrupted",
    },
    "synthesizing": {
        "completed",
        "completed_partial",
        "waiting_approval",
        "failed",
        "stopped",
        "interrupted",
    },
    "interrupted": {"running", "stopped"},
    "completed": set(),
    "completed_partial": set(),
    "failed": set(),
    "stopped": set(),
}


class OrchestrationError(ValueError):
    """Raised when orchestration state or a requested transition is invalid."""


@dataclass(frozen=True)
class ThreadEvent:
    """One ordered, idempotent model-visible input for a durable parent turn."""

    id: str
    orchestration_id: str
    kind: str
    content: str
    payload: dict[str, Any]
    run_id: str = ""
    source_event_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ParentPassResult:
    """Classification applied after one pass of the original parent graph."""

    orchestration_id: str
    parent_state: str
    output_kind: str
    waiting: bool
    text: str


def orchestration_status_label(status: str) -> str:
    """Return compact user-facing copy for a durable group status."""

    clean = str(status or "").strip()
    return ORCHESTRATION_STATUS_LABELS.get(
        clean,
        clean.replace("_", " ").strip().title() or "Unknown",
    )


def agent_member_status_label(status: str) -> str:
    """Return compact user-facing copy for a child Agent status."""

    clean = str(status or "").strip()
    return AGENT_MEMBER_STATUS_LABELS.get(
        clean,
        clean.replace("_", " ").strip().title() or "Unknown",
    )


def _emit_orchestration_buddy_event(
    orchestration: Mapping[str, Any],
    *,
    terminal: bool,
) -> None:
    orchestration_id = str(orchestration.get("id") or "")
    if not orchestration_id:
        return
    try:
        from row_bot.buddy.events import BuddyEventType, emit_buddy_event

        thread_id = str(orchestration.get("parent_thread_id") or "")
        if terminal:
            emit_buddy_event(
                BuddyEventType.ORCHESTRATION_DONE,
                source="agent_orchestrator",
                payload={
                    "orchestration_id": orchestration_id,
                    "thread_id": thread_id,
                    "label": "Agent work done",
                },
            )
        activity = get_thread_orchestration_activity([thread_id]).get(thread_id, {})
        if str(activity.get("state") or "") == "active":
            emit_buddy_event(
                BuddyEventType.ORCHESTRATION_ACTIVE,
                source="agent_orchestrator",
                payload={
                    "orchestration_id": orchestration_id,
                    "thread_id": thread_id,
                    **activity,
                },
            )
    except Exception:
        logger.debug("Could not publish orchestration Buddy lifecycle", exc_info=True)


def _now() -> str:
    return datetime.now().isoformat()


def _conn():
    from row_bot.tasks import _get_conn

    return _get_conn()


def _ensure_schema() -> None:
    from row_bot.agent_runs import ensure_agent_run_schema

    ensure_agent_run_schema()


def _json_text(value: Mapping[str, Any] | Sequence[Any] | None) -> str:
    if value is None:
        value = {}
    return json.dumps(value, sort_keys=True)


def _parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item or "").strip()]


def _orchestration_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for name in (
        "continuation_state_json",
        "delivery_context_json",
        "settings_snapshot_json",
    ):
        result[name] = _parse_object(result.get(name))
    for name in (
        "required_total",
        "optional_total",
        "acknowledgement_sent",
        "orchestration_version",
        "parent_attempt",
    ):
        result[name] = int(result.get(name) or 0)
    return result


def _member_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["required"] = bool(result.get("required"))
    result["wave"] = int(result.get("wave") or 0)
    result["sequence"] = int(result.get("sequence") or 0)
    result["attempt"] = int(result.get("attempt") or 1)
    result["dependency_run_ids_json"] = _parse_list(
        result.get("dependency_run_ids_json")
    )
    return result


def _message_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["payload_json"] = _parse_object(result.get("payload_json"))
    result["attempt_count"] = int(result.get("attempt_count") or 0)
    return result


def set_test_executors(
    *,
    synthesis: Callable[[dict[str, Any], str], str] | None = None,
    parent: Callable[
        [dict[str, Any], str, list[str], dict[str, Any]], str | dict[str, Any]
    ] | None = None,
    retry: Callable[[dict[str, Any], dict[str, Any], bool], dict[str, Any]] | None = None,
    delivery: Callable[[dict[str, Any], str, str, str], bool] | None = None,
) -> None:
    """Install deterministic service executors; passing no callbacks resets them."""

    global _SYNTHESIS_EXECUTOR, _PARENT_EXECUTOR, _RETRY_EXECUTOR, _DELIVERY_EXECUTOR
    with _SERVICE_LOCK:
        _SYNTHESIS_EXECUTOR = synthesis
        _PARENT_EXECUTOR = parent
        _RETRY_EXECUTOR = retry
        _DELIVERY_EXECUTOR = delivery


def create_or_get_orchestration(
    *,
    parent_thread_id: str,
    parent_generation_id: str,
    root_objective: str,
    model_ref: str,
    approval_mode: str,
    runtime_surface: str,
    parent_run_id: str = "",
    settings_snapshot: Mapping[str, Any] | None = None,
    continuation_state: Mapping[str, Any] | None = None,
    delivery_context: Mapping[str, Any] | None = None,
    orchestration_version: int = 1,
) -> dict[str, Any]:
    """Create the one orchestration owned by a parent generation."""

    _ensure_schema()
    parent_thread_id = str(parent_thread_id or "").strip()
    parent_generation_id = str(parent_generation_id or "").strip()
    root_objective = str(root_objective or "").strip()
    model_ref = str(model_ref or "").strip()
    if not parent_thread_id or not parent_generation_id:
        raise OrchestrationError("Parent thread and generation are required.")
    if not root_objective:
        raise OrchestrationError("The parent objective is required.")
    if not model_ref:
        raise OrchestrationError("The parent model snapshot is required.")
    from row_bot.agent_runs import get_agent_settings_snapshot

    snapshot = get_agent_settings_snapshot(settings_snapshot)
    orchestration_id = uuid.uuid4().hex[:12]
    version = max(1, int(orchestration_version or 1))
    parent_state = "running" if version >= CURRENT_ORCHESTRATION_VERSION else ""
    now = _now()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM agent_orchestrations "
            "WHERE parent_thread_id = ? AND parent_generation_id = ?",
            (parent_thread_id, parent_generation_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO agent_orchestrations "
                "(id, parent_thread_id, parent_generation_id, parent_run_id, "
                "root_objective, status, model_ref, approval_mode, runtime_surface, "
                "required_total, optional_total, acknowledgement_sent, "
                "continuation_state_json, delivery_context_json, "
                "settings_snapshot_json, orchestration_version, parent_state, "
                "created_at, updated_at, completed_at, "
                "error_message) VALUES (?, ?, ?, ?, ?, 'planning', ?, ?, ?, 0, 0, "
                "0, ?, ?, ?, ?, ?, ?, ?, '', '')",
                (
                    orchestration_id,
                    parent_thread_id,
                    parent_generation_id,
                    str(parent_run_id or ""),
                    root_objective,
                    model_ref,
                    str(approval_mode or ""),
                    str(runtime_surface or "chat"),
                    _json_text(dict(continuation_state or {})),
                    _json_text(dict(delivery_context or {})),
                    _json_text(snapshot),
                    version,
                    parent_state,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_orchestrations WHERE id = ?",
                (orchestration_id,),
            ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _orchestration_row(row)
    if not result:
        raise OrchestrationError("Could not create the orchestration.")
    return result


def get_orchestration(orchestration_id: str) -> dict[str, Any] | None:
    _ensure_schema()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_orchestrations WHERE id = ?",
            (str(orchestration_id),),
        ).fetchone()
    finally:
        conn.close()
    return _orchestration_row(row)


def transition_orchestration(
    orchestration_id: str,
    status: str,
    *,
    error_message: str = "",
) -> dict[str, Any]:
    """Apply one validated orchestration lifecycle transition."""

    status = str(status or "").strip()
    if status not in ORCHESTRATION_STATUSES:
        raise OrchestrationError(f"Unknown orchestration status: {status}")
    current = get_orchestration(orchestration_id)
    if not current:
        raise OrchestrationError("Orchestration not found.")
    current_status = str(current.get("status") or "")
    if status == current_status:
        return current
    if status not in _LEGAL_TRANSITIONS.get(current_status, set()):
        raise OrchestrationError(
            f"Illegal orchestration transition: {current_status} -> {status}"
        )
    completed_at = (
        _now()
        if status in {"completed", "completed_partial", "failed", "stopped"}
        else ""
    )
    conn = _conn()
    try:
        changed = conn.execute(
            "UPDATE agent_orchestrations SET status = ?, error_message = ?, "
            "completed_at = CASE WHEN ? != '' THEN ? ELSE completed_at END, "
            "updated_at = ? WHERE id = ? AND status = ?",
            (
                status,
                str(error_message or ""),
                completed_at,
                completed_at,
                _now(),
                orchestration_id,
                current_status,
            ),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if not changed:
        raise OrchestrationError("Orchestration changed concurrently; retry the action.")
    result = get_orchestration(orchestration_id)
    assert result is not None
    _emit_orchestration_buddy_event(
        result,
        terminal=status in {"completed", "completed_partial", "failed", "stopped"},
    )
    return result


def get_generation_orchestration(
    parent_thread_id: str,
    parent_generation_id: str,
) -> dict[str, Any] | None:
    _ensure_schema()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_orchestrations "
            "WHERE parent_thread_id = ? AND parent_generation_id = ?",
            (str(parent_thread_id), str(parent_generation_id)),
        ).fetchone()
    finally:
        conn.close()
    return _orchestration_row(row)


def get_active_orchestration(parent_thread_id: str) -> dict[str, Any] | None:
    _ensure_schema()
    statuses = sorted(ACTIVE_ORCHESTRATION_STATUSES | {"interrupted"})
    placeholders = ", ".join("?" for _ in statuses)
    conn = _conn()
    try:
        row = conn.execute(
            f"SELECT * FROM agent_orchestrations WHERE parent_thread_id = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            (str(parent_thread_id), *statuses),
        ).fetchone()
    finally:
        conn.close()
    return _orchestration_row(row)


def list_orchestrations(
    *,
    parent_thread_id: str = "",
    statuses: Sequence[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    _ensure_schema()
    clauses: list[str] = []
    params: list[Any] = []
    if parent_thread_id:
        clauses.append("parent_thread_id = ?")
        params.append(str(parent_thread_id))
    clean_statuses = [str(status) for status in (statuses or []) if str(status)]
    if clean_statuses:
        clauses.append(f"status IN ({', '.join('?' for _ in clean_statuses)})")
        params.extend(clean_statuses)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(200, int(limit or 20))))
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM agent_orchestrations{where} "
            "ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        parsed for row in rows if (parsed := _orchestration_row(row)) is not None
    ]


def get_thread_orchestration_activity(
    parent_thread_ids: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return compact durable orchestration activity keyed by parent thread."""

    _ensure_schema()
    clean_thread_ids = [
        str(thread_id).strip()
        for thread_id in (parent_thread_ids or [])
        if str(thread_id or "").strip()
    ]
    clauses = ""
    params: list[Any] = []
    if clean_thread_ids:
        clauses = (
            f"WHERE parent_thread_id IN ({', '.join('?' for _ in clean_thread_ids)})"
        )
        params.extend(clean_thread_ids)
    active_statuses = sorted(ACTIVE_ORCHESTRATION_STATUSES | {"interrupted"})
    active_placeholders = ", ".join("?" for _ in active_statuses)
    conn = _conn()
    try:
        rows = conn.execute(
            "WITH ranked AS ("
            "SELECT *, ROW_NUMBER() OVER (PARTITION BY parent_thread_id ORDER BY "
            f"CASE WHEN status IN ({active_placeholders}) THEN 0 ELSE 1 END, "
            "updated_at DESC, created_at DESC, id DESC) AS activity_rank "
            f"FROM agent_orchestrations {clauses}) "
            "SELECT * FROM ranked WHERE activity_rank = 1",
            (*active_statuses, *params),
        ).fetchall()
        orchestration_rows = [
            parsed
            for row in rows
            if (parsed := _orchestration_row(row)) is not None
        ]
        orchestration_ids = [str(row.get("id") or "") for row in orchestration_rows]
        member_rows: list[Any] = []
        approval_rows: list[Any] = []
        if orchestration_ids:
            member_rows = conn.execute(
                "SELECT m.orchestration_id, m.run_id, m.required, "
                "m.status AS member_status, "
                "r.status AS run_status, r.stop_requested "
                "FROM agent_orchestration_members m "
                "LEFT JOIN agent_runs r ON r.id = m.run_id "
                f"WHERE m.orchestration_id IN ({', '.join('?' for _ in orchestration_ids)})",
                orchestration_ids,
            ).fetchall()
            approval_thread_ids = [
                str(row.get("parent_thread_id") or "")
                for row in orchestration_rows
                if str(row.get("parent_thread_id") or "")
            ]
            if approval_thread_ids:
                approval_rows = conn.execute(
                    "SELECT id, status, parent_thread_id, agent_run_id, step_id "
                    "FROM approval_requests WHERE status = 'pending' "
                    f"AND parent_thread_id IN ({', '.join('?' for _ in approval_thread_ids)})",
                    approval_thread_ids,
                ).fetchall()
    finally:
        conn.close()

    members_by_orchestration: dict[str, list[dict[str, Any]]] = {}
    for row in member_rows:
        member = dict(row)
        members_by_orchestration.setdefault(
            str(member.get("orchestration_id") or ""), []
        ).append(member)

    pending_by_thread: dict[str, list[dict[str, Any]]] = {}
    for row in approval_rows:
        approval = dict(row)
        pending_by_thread.setdefault(
            str(approval.get("parent_thread_id") or ""), []
        ).append(approval)

    activity_by_thread: dict[str, dict[str, Any]] = {}
    inactive_member_statuses = TERMINAL_MEMBER_STATUSES | {
        "cleared",
        "transferred",
    }
    for orchestration in orchestration_rows:
        orchestration_id = str(orchestration.get("id") or "")
        members = members_by_orchestration.get(orchestration_id, [])
        current_members: list[tuple[dict[str, Any], str]] = []
        for member in members:
            member_status = str(member.get("member_status") or "")
            if member_status in {"retried", "transferred", "cleared"}:
                continue
            run_status = str(member.get("run_status") or "")
            effective_status = run_status or member_status
            current_members.append((member, effective_status))

        active_members = [
            (member, status)
            for member, status in current_members
            if status not in inactive_member_statuses
        ]
        failed_members = [
            (member, status)
            for member, status in current_members
            if status in FAILED_MEMBER_STATUSES
        ]
        required_active = [
            (member, status)
            for member, status in active_members
            if bool(member.get("required"))
        ]
        optional_active = [
            (member, status)
            for member, status in active_members
            if not bool(member.get("required"))
        ]
        orchestration_status = str(orchestration.get("status") or "")
        parent_state = str(orchestration.get("parent_state") or "")
        parent_thread_id = str(orchestration.get("parent_thread_id") or "")
        current_run_ids = {
            str(member.get("run_id") or "")
            for member, _status in current_members
            if str(member.get("run_id") or "")
        }
        pending_approvals = [
            approval
            for approval in pending_by_thread.get(parent_thread_id, [])
            if (
                str(approval.get("step_id") or "")
                == f"orchestration:{orchestration_id}"
                or str(approval.get("agent_run_id") or "") in current_run_ids
            )
        ]
        has_pending_approval = bool(pending_approvals)
        missing_parent_approval = (
            orchestration_status == "waiting_approval"
            or parent_state == "waiting_approval"
        ) and not has_pending_approval
        interrupted = orchestration_status == "interrupted"
        blocking = orchestration_status in ACTIVE_ORCHESTRATION_STATUSES
        if interrupted or missing_parent_approval:
            blocking = False
        background = (
            bool(optional_active)
            and not blocking
            and not interrupted
            and not missing_parent_approval
        )
        active = blocking or background
        effective_statuses = [status for _member, status in current_members]
        if interrupted or missing_parent_approval:
            state = "attention"
            phase = "resume_required"
        elif not active:
            state = "terminal"
            phase = orchestration_status or "terminal"
        elif has_pending_approval:
            state = "active"
            phase = "approval_wait"
        elif any(
            bool(member.get("stop_requested"))
            for member, _status in active_members
        ):
            state = "active"
            phase = "stopping"
        elif (
            "retrying" in effective_statuses
            or (blocking and bool(failed_members))
        ):
            state = "active"
            phase = "retry"
        elif background:
            state = "active"
            phase = "background"
        elif (
            orchestration_status == "synthesizing"
            or parent_state in {"running", "runnable"}
            and int(orchestration.get("parent_attempt") or 0) > 0
        ):
            state = "active"
            phase = "later_wave_parent"
        elif required_active:
            state = "active"
            phase = "child_running"
        else:
            state = "active"
            phase = "later_wave_parent"

        activity_by_thread[parent_thread_id] = {
            "orchestration_id": orchestration_id,
            "state": state,
            "blocking": bool(blocking),
            "background": bool(background),
            "phase": phase,
            "active_members": len(active_members),
            "failed_members": len(failed_members),
        }
    return activity_by_thread


def get_member_for_run(run_id: str) -> dict[str, Any] | None:
    _ensure_schema()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_orchestration_members WHERE run_id = ? "
            "AND status != 'transferred' ORDER BY rowid DESC LIMIT 1",
            (str(run_id),),
        ).fetchone()
    finally:
        conn.close()
    return _member_row(row)


def list_members(
    orchestration_id: str,
    *,
    include_runs: bool = True,
) -> list[dict[str, Any]]:
    _ensure_schema()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_orchestration_members "
            "WHERE orchestration_id = ? ORDER BY sequence, attempt, run_id",
            (str(orchestration_id),),
        ).fetchall()
        members = [
            parsed for row in rows if (parsed := _member_row(row)) is not None
        ]
        if include_runs:
            for member in members:
                run = conn.execute(
                    "SELECT * FROM agent_runs WHERE id = ?",
                    (member["run_id"],),
                ).fetchone()
                member["run"] = dict(run) if run is not None else {}
    finally:
        conn.close()
    return members


def has_duplicate_objective(orchestration_id: str, objective: str) -> bool:
    clean = " ".join(str(objective or "").lower().split())
    if not clean:
        return False
    for member in list_members(orchestration_id):
        if str(member.get("retry_of_run_id") or ""):
            continue
        run = member.get("run") or {}
        if " ".join(str(run.get("prompt") or "").lower().split()) == clean:
            return True
    return False


def _validated_dependencies(
    conn: Any,
    orchestration_id: str,
    run_id: str,
    dependency_run_ids: Sequence[str] | None,
) -> list[str]:
    dependencies: list[str] = []
    seen: set[str] = set()
    for raw in dependency_run_ids or ():
        dependency = str(raw or "").strip()
        if not dependency or dependency in seen:
            continue
        if dependency == str(run_id):
            raise OrchestrationError("A child cannot depend on itself.")
        row = conn.execute(
            "SELECT 1 FROM agent_orchestration_members "
            "WHERE orchestration_id = ? AND run_id = ?",
            (orchestration_id, dependency),
        ).fetchone()
        if row is None:
            raise OrchestrationError(
                "Child dependencies must belong to the same orchestration."
            )
        seen.add(dependency)
        dependencies.append(dependency)
    return dependencies


def register_member(
    orchestration_id: str,
    run_id: str,
    *,
    required: bool = True,
    dependency_run_ids: Sequence[str] | None = None,
    attempt: int = 1,
    retry_of_run_id: str = "",
) -> dict[str, Any]:
    """Attach an existing run to the orchestration before its worker starts."""

    _ensure_schema()
    orchestration_id = str(orchestration_id or "").strip()
    run_id = str(run_id or "").strip()
    if not orchestration_id or not run_id:
        raise OrchestrationError("Orchestration and run ids are required.")
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        orchestration = conn.execute(
            "SELECT * FROM agent_orchestrations WHERE id = ?",
            (orchestration_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT status FROM agent_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if orchestration is None or run is None:
            raise OrchestrationError("The orchestration or Agent Run does not exist.")
        if str(orchestration["status"] or "") in {
            "completed",
            "completed_partial",
            "failed",
            "stopped",
        }:
            raise OrchestrationError("The orchestration is already terminal.")
        dependencies = _validated_dependencies(
            conn,
            orchestration_id,
            run_id,
            dependency_run_ids,
        )
        prior = conn.execute(
            "SELECT * FROM agent_orchestration_members "
            "WHERE orchestration_id = ? AND run_id = ?",
            (orchestration_id, run_id),
        ).fetchone()
        if prior is not None:
            conn.commit()
            parsed = _member_row(prior)
            assert parsed is not None
            return parsed
        sequence_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 AS value "
            "FROM agent_orchestration_members WHERE orchestration_id = ?",
            (orchestration_id,),
        ).fetchone()
        sequence = int(sequence_row["value"] or 0)
        snapshot = _parse_object(orchestration["settings_snapshot_json"])
        per_parent = max(1, int(snapshot.get("max_concurrent_children") or 3))
        wave = sequence // per_parent
        logical_required = bool(required)
        logical_optional = not logical_required
        retry_of_run_id = str(retry_of_run_id or "").strip()
        if retry_of_run_id:
            replaced = conn.execute(
                "SELECT required FROM agent_orchestration_members "
                "WHERE orchestration_id = ? AND run_id = ?",
                (orchestration_id, retry_of_run_id),
            ).fetchone()
            if replaced is None:
                raise OrchestrationError("The retry source is not in this orchestration.")
            logical_required = bool(replaced["required"])
            logical_optional = not logical_required
            conn.execute(
                "UPDATE agent_orchestration_members SET required = 0, "
                "status = CASE WHEN status = 'retrying' THEN 'retried' ELSE status END "
                "WHERE orchestration_id = ? AND run_id = ?",
                (orchestration_id, retry_of_run_id),
            )
        conn.execute(
            "INSERT INTO agent_orchestration_members "
            "(orchestration_id, run_id, required, wave, sequence, attempt, "
            "retry_of_run_id, dependency_run_ids_json, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                orchestration_id,
                run_id,
                1 if logical_required else 0,
                wave,
                sequence,
                max(1, int(attempt or 1)),
                retry_of_run_id,
                _json_text(dependencies),
                str(run["status"] or "queued"),
            ),
        )
        if not retry_of_run_id:
            conn.execute(
                "UPDATE agent_orchestrations SET required_total = required_total + ?, "
                "optional_total = optional_total + ?, "
                "status = CASE WHEN status = 'planning' THEN 'running' ELSE status END, "
                "updated_at = ? WHERE id = ?",
                (
                    1 if logical_required else 0,
                    1 if logical_optional else 0,
                    _now(),
                    orchestration_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE agent_orchestrations SET "
                "status = CASE WHEN status = 'interrupted' THEN 'running' ELSE status END, "
                "updated_at = ? WHERE id = ?",
                (_now(), orchestration_id),
            )
        row = conn.execute(
            "SELECT * FROM agent_orchestration_members "
            "WHERE orchestration_id = ? AND run_id = ?",
            (orchestration_id, run_id),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    parsed = _member_row(row)
    if not parsed:
        raise OrchestrationError("Could not register the child member.")
    if (
        _is_unified_parent(_orchestration_row(orchestration))
        and str(run["status"] or "") in TERMINAL_MEMBER_STATUSES
    ):
        # Structured waits may join a run that finished before the barrier was
        # armed. Materialize its terminal event now; request_parent_wake will
        # defer execution until arm_parent_wait moves the parent to waiting.
        handle_run_terminal(run_id)
    return parsed


def transfer_member(
    target_orchestration_id: str,
    run_id: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    """Move a durable logical member to a later barrier while retaining history."""

    _ensure_schema()
    source = get_member_for_run(run_id)
    if not source:
        return register_member(target_orchestration_id, run_id, required=required)
    if str(source["orchestration_id"]) == str(target_orchestration_id):
        if bool(source.get("required")) == bool(required):
            return source
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE agent_orchestration_members SET required = ? "
                "WHERE orchestration_id = ? AND run_id = ?",
                (
                    1 if required else 0,
                    target_orchestration_id,
                    run_id,
                ),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE agent_orchestrations SET "
                    "required_total = required_total + ?, "
                    "optional_total = MAX(0, optional_total + ?), updated_at = ? "
                    "WHERE id = ?",
                    (
                        1 if required else -1,
                        -1 if required else 1,
                        _now(),
                        target_orchestration_id,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        promoted = get_member_for_run(run_id)
        if not promoted:
            raise OrchestrationError("Could not update orchestration membership.")
        return promoted

    source_id = str(source["orchestration_id"])
    if source.get("required"):
        raise OrchestrationError(
            "A required child already belongs to another active barrier."
        )
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT * FROM agent_orchestrations WHERE id = ?",
            (str(target_orchestration_id),),
        ).fetchone()
        run = conn.execute(
            "SELECT status FROM agent_runs WHERE id = ?",
            (str(run_id),),
        ).fetchone()
        if target is None or run is None:
            raise OrchestrationError("The target orchestration or Agent Run does not exist.")
        if str(target["status"] or "") in {
            "completed",
            "completed_partial",
            "failed",
            "stopped",
        }:
            raise OrchestrationError("The target orchestration is already terminal.")
        sequence_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 AS value "
            "FROM agent_orchestration_members WHERE orchestration_id = ?",
            (str(target_orchestration_id),),
        ).fetchone()
        sequence = int(sequence_row["value"] or 0)
        snapshot = _parse_object(target["settings_snapshot_json"])
        per_parent = max(1, int(snapshot.get("max_concurrent_children") or 3))
        conn.execute(
            "UPDATE agent_orchestration_members SET required = 0, status = 'transferred' "
            "WHERE orchestration_id = ? AND run_id = ?",
            (source_id, str(run_id)),
        )
        conn.execute(
            "UPDATE agent_orchestrations SET "
            "required_total = MAX(0, required_total - ?), "
            "optional_total = MAX(0, optional_total - ?), updated_at = ? WHERE id = ?",
            (
                1 if source.get("required") else 0,
                0 if source.get("required") else 1,
                _now(),
                source_id,
            ),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS value FROM agent_orchestration_members "
            "WHERE orchestration_id = ? AND status != 'transferred'",
            (source_id,),
        ).fetchone()
        if int(remaining["value"] or 0) == 0:
            conn.execute(
                "UPDATE agent_orchestrations SET status = 'completed', completed_at = ?, "
                "updated_at = ? WHERE id = ? AND status NOT IN "
                "('failed', 'stopped', 'completed', 'completed_partial')",
                (_now(), _now(), source_id),
            )
        conn.execute(
            "INSERT INTO agent_orchestration_members "
            "(orchestration_id, run_id, required, wave, sequence, attempt, "
            "retry_of_run_id, dependency_run_ids_json, status) "
            "VALUES (?, ?, ?, ?, ?, 1, '', '[]', ?)",
            (
                str(target_orchestration_id),
                str(run_id),
                1 if required else 0,
                sequence // per_parent,
                sequence,
                str(run["status"] or "queued"),
            ),
        )
        conn.execute(
            "UPDATE agent_orchestrations SET required_total = required_total + ?, "
            "optional_total = optional_total + ?, "
            "status = CASE WHEN status = 'planning' THEN 'running' ELSE status END, "
            "updated_at = ? WHERE id = ?",
            (
                1 if required else 0,
                0 if required else 1,
                _now(),
                str(target_orchestration_id),
            ),
        )
        row = conn.execute(
            "SELECT * FROM agent_orchestration_members "
            "WHERE orchestration_id = ? AND run_id = ?",
            (str(target_orchestration_id), str(run_id)),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    parsed = _member_row(row)
    if not parsed:
        raise OrchestrationError("Could not transfer orchestration membership.")
    if (
        _is_unified_parent(_orchestration_row(target))
        and str(run["status"] or "") in TERMINAL_MEMBER_STATUSES
    ):
        handle_run_terminal(run_id)
    return parsed


def dependencies_ready(run_id: str) -> bool:
    member = get_member_for_run(run_id)
    if not member:
        return True
    dependencies = member.get("dependency_run_ids_json") or []
    if not dependencies:
        return True
    _ensure_schema()
    conn = _conn()
    try:
        placeholders = ", ".join("?" for _ in dependencies)
        rows = conn.execute(
            f"SELECT run_id, status FROM agent_orchestration_members "
            f"WHERE orchestration_id = ? AND run_id IN ({placeholders})",
            (member["orchestration_id"], *dependencies),
        ).fetchall()
    finally:
        conn.close()
    statuses = {str(row["run_id"]): str(row["status"]) for row in rows}
    return all(statuses.get(run_id) in TERMINAL_MEMBER_STATUSES for run_id in dependencies)


def wait_for_dependencies(run_id: str, stop_event: threading.Event) -> bool:
    """Wait eventfully for member dependencies without consuming capacity."""

    while not stop_event.is_set():
        if dependencies_ready(run_id):
            return True
        with _SERVICE_LOCK:
            event = _DEPENDENCY_EVENTS.setdefault(str(run_id), threading.Event())
        event.wait()
        event.clear()
    return False


_DEPENDENCY_EVENTS: dict[str, threading.Event] = {}


def _wake_dependency_waiters(orchestration_id: str) -> None:
    for member in list_members(orchestration_id, include_runs=False):
        dependencies = member.get("dependency_run_ids_json") or []
        if dependencies:
            with _SERVICE_LOCK:
                event = _DEPENDENCY_EVENTS.get(str(member["run_id"]))
            if event is not None:
                event.set()


def _member_counts(orchestration_id: str) -> dict[str, int]:
    members = list_members(orchestration_id)
    current_members = [member for member in members if member.get("required")]

    def effective_status(member: dict[str, Any]) -> str:
        member_status = str(member.get("status") or "")
        run_status = str((member.get("run") or {}).get("status") or "")
        if run_status in TERMINAL_MEMBER_STATUSES or run_status == "waiting_approval":
            return run_status
        return member_status or run_status

    statuses = [effective_status(member) for member in current_members]
    running = sum(
        status in {"queued", "running", "waiting_user", "paused"}
        for status in statuses
    )
    needs_approval = sum(status == "waiting_approval" for status in statuses)
    return {
        "running": running,
        "needs_approval": needs_approval,
        "completed": sum(
            status in {"completed", "completed_delivery_failed"} for status in statuses
        ),
        "failed": sum(status in FAILED_MEMBER_STATUSES for status in statuses),
        "active": running + needs_approval,
        "required": len(current_members),
        "total_attempts": len(members),
    }


def orchestration_overview(orchestration_id: str) -> dict[str, Any]:
    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        return {}
    return {
        **orchestration,
        "counts": _member_counts(orchestration_id),
        "members": list_members(orchestration_id),
    }


def record_message(
    orchestration_id: str,
    *,
    kind: str,
    content: str,
    run_id: str = "",
    message_id: str = "",
    delivery_status: str = "pending",
    payload: Mapping[str, Any] | None = None,
    source_event_id: str = "",
) -> dict[str, Any]:
    _ensure_schema()
    message_id = str(message_id or uuid.uuid4().hex[:12])
    now = _now()
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agent_orchestration_messages "
            "(id, orchestration_id, run_id, kind, content, payload_json, "
            "source_event_id, delivery_status, created_at, delivered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                str(orchestration_id),
                str(run_id or ""),
                str(kind or "steering"),
                str(content or ""),
                _json_text(dict(payload or {})),
                str(source_event_id or ""),
                str(delivery_status or "pending"),
                now,
                now if delivery_status == "delivered" else "",
            ),
        )
        row = conn.execute(
            "SELECT * FROM agent_orchestration_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    return _message_row(row) or {}


def list_messages(
    orchestration_id: str,
    *,
    kinds: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    _ensure_schema()
    params: list[Any] = [str(orchestration_id)]
    clause = ""
    clean_kinds = [str(kind) for kind in (kinds or []) if str(kind)]
    if clean_kinds:
        clause = f" AND kind IN ({', '.join('?' for _ in clean_kinds)})"
        params.extend(clean_kinds)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_orchestration_messages "
            f"WHERE orchestration_id = ?{clause} ORDER BY created_at, id",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_message_row(row) or {} for row in rows]


def _is_unified_parent(orchestration: Mapping[str, Any] | None) -> bool:
    return bool(
        orchestration
        and int(orchestration.get("orchestration_version") or 0)
        >= CURRENT_ORCHESTRATION_VERSION
    )


def record_thread_event(
    orchestration_id: str,
    *,
    kind: str,
    content: str,
    source_event_id: str,
    run_id: str = "",
    payload: Mapping[str, Any] | None = None,
    request_wake: bool = True,
) -> ThreadEvent:
    """Persist one idempotent input for the original parent thread."""

    orchestration = get_orchestration(orchestration_id)
    if not _is_unified_parent(orchestration):
        raise OrchestrationError("Thread events require a version 2 orchestration.")
    clean_kind = str(kind or "").strip()
    if clean_kind not in THREAD_EVENT_KINDS:
        raise OrchestrationError(f"Unknown thread event kind: {clean_kind}")
    source_event_id = str(source_event_id or "").strip()
    if not source_event_id:
        raise OrchestrationError("A stable source event id is required.")
    message_id = f"orchestration:{orchestration_id}:event:{source_event_id}"
    row = record_message(
        orchestration_id,
        kind=f"event.{clean_kind}",
        content=str(content or ""),
        run_id=run_id,
        message_id=message_id,
        delivery_status="pending",
        payload=payload,
        source_event_id=source_event_id,
    )
    event = ThreadEvent(
        id=str(row.get("id") or message_id),
        orchestration_id=str(orchestration_id),
        kind=clean_kind,
        content=str(row.get("content") or ""),
        payload=dict(row.get("payload_json") or {}),
        run_id=str(row.get("run_id") or ""),
        source_event_id=str(row.get("source_event_id") or source_event_id),
        created_at=str(row.get("created_at") or ""),
    )
    if request_wake:
        request_parent_wake(orchestration_id)
    return event


def pending_thread_events(orchestration_id: str) -> list[ThreadEvent]:
    """Return ordered unconsumed inputs for one durable parent turn."""

    rows = list_messages(orchestration_id)
    return [
        ThreadEvent(
            id=str(row.get("id") or ""),
            orchestration_id=str(row.get("orchestration_id") or orchestration_id),
            kind=str(row.get("kind") or "").removeprefix("event."),
            content=str(row.get("content") or ""),
            payload=dict(row.get("payload_json") or {}),
            run_id=str(row.get("run_id") or ""),
            source_event_id=str(row.get("source_event_id") or ""),
            created_at=str(row.get("created_at") or ""),
        )
        for row in rows
        if str(row.get("kind") or "").startswith("event.")
        and not str(row.get("consumed_at") or "")
    ]


def _parent_wake_ready(orchestration_id: str) -> bool:
    """Return whether durable inputs justify one detached recovery pass."""

    events = pending_thread_events(orchestration_id)
    if any(
        event.kind
        in {
            "parent_steering",
            "stop_requested",
            "goal_continuation",
            "workflow_continuation",
        }
        for event in events
    ):
        return True
    return bool(events) and _barrier_ready(orchestration_id)


def sanitize_pending_child_event_ids(
    orchestration_id: str,
    event_ids: Sequence[str] | None,
) -> list[str]:
    """Keep only currently pending child lifecycle events from this orchestration."""

    requested = {str(event_id) for event_id in event_ids or () if str(event_id)}
    if not requested:
        return []
    return [
        event.id
        for event in pending_thread_events(orchestration_id)
        if event.id in requested and event.kind in CHILD_LIFECYCLE_EVENT_KINDS
    ]


def _required_group_event_ids(
    orchestration_id: str,
    members: Sequence[Mapping[str, Any]],
) -> list[str]:
    current_run_ids = {
        str(member.get("run_id") or "")
        for member in members
        if member.get("required") and str(member.get("run_id") or "")
    }
    lineage_run_ids = set(current_run_ids)
    by_run_id = {
        str(member.get("run_id") or ""): member
        for member in list_members(orchestration_id, include_runs=False)
    }
    pending = list(current_run_ids)
    while pending:
        member = by_run_id.get(pending.pop()) or {}
        prior_run_id = str(member.get("retry_of_run_id") or "")
        if prior_run_id and prior_run_id not in lineage_run_ids:
            lineage_run_ids.add(prior_run_id)
            pending.append(prior_run_id)
    return [
        event.id
        for event in pending_thread_events(orchestration_id)
        if event.kind in CHILD_LIFECYCLE_EVENT_KINDS
        and (
            event.run_id in lineage_run_ids
            or str(event.payload.get("failed_run_id") or "") in lineage_run_ids
            or str(event.payload.get("replacement_run_id") or "") in lineage_run_ids
        )
    ]


def wait_for_required_group(
    orchestration_id: str,
    timeout: float | None = 60.0,
) -> dict[str, Any]:
    """Join the current required cohort without invoking a provider."""

    import time

    from row_bot import agent_runner

    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        raise OrchestrationError("Orchestration not found.")
    deadline = (
        time.monotonic() + max(0.0, float(timeout))
        if timeout is not None
        else None
    )

    while True:
        members = [
            member
            for member in list_members(orchestration_id)
            if member.get("required")
        ]
        outstanding = [
            member
            for member in members
            if str(
                (member.get("run") or {}).get("status")
                or member.get("status")
                or ""
            )
            not in TERMINAL_MEMBER_STATUSES
        ]
        if not outstanding:
            break
        remaining = (
            None if deadline is None else max(0.0, deadline - time.monotonic())
        )
        if remaining is not None and remaining <= 0:
            break
        awaited = outstanding[0]
        agent_runner.wait_for_agent_run_terminal(
            str(awaited["run_id"]),
            timeout=remaining,
        )
        refreshed = get_member_for_run(str(awaited["run_id"])) or awaited
        refreshed_run = next(
            (
                member.get("run") or {}
                for member in list_members(orchestration_id)
                if str(member.get("run_id") or "") == str(awaited["run_id"])
            ),
            {},
        )
        refreshed_status = str(
            refreshed_run.get("status") or refreshed.get("status") or ""
        )
        if (
            refreshed_status not in TERMINAL_MEMBER_STATUSES
            and deadline is not None
            and time.monotonic() >= deadline
        ):
            break

    members = [
        member
        for member in list_members(orchestration_id)
        if member.get("required")
    ]
    runs: list[dict[str, Any]] = []
    outstanding_run_ids: list[str] = []
    for member in members:
        run = dict(member.get("run") or {})
        status = str(run.get("status") or member.get("status") or "")
        if not run:
            run = {"id": str(member.get("run_id") or ""), "status": status}
        if status not in TERMINAL_MEMBER_STATUSES:
            outstanding_run_ids.append(str(member.get("run_id") or ""))
        runs.append(run)
    required_total = int(orchestration.get("required_total") or 0)
    barrier_complete = (
        required_total > 0
        and len(members) == required_total
        and not outstanding_run_ids
    )
    return {
        "orchestration_id": str(orchestration_id),
        "runs": runs,
        "barrier_complete": barrier_complete,
        "timed_out": bool(outstanding_run_ids),
        "outstanding_run_ids": outstanding_run_ids,
        "child_event_ids": _required_group_event_ids(orchestration_id, members),
    }


def _format_thread_events(
    orchestration: Mapping[str, Any],
    events: Sequence[ThreadEvent],
    *,
    limit: int = 24000,
) -> str:
    """Create bounded factual context, not user-facing narration."""

    blocks = [
        "Thread orchestration events for the same parent turn.",
        f"Original objective: {orchestration.get('root_objective', '')}",
        "Continue as the original parent. Use the event facts below, delegate "
        "later work if useful, and answer the user naturally.",
    ]
    for event in events:
        payload = json.dumps(event.payload, sort_keys=True, ensure_ascii=False)
        blocks.extend(
            [
                "",
                f"Message Type: {event.kind.upper()}",
                f"Event ID: {event.source_event_id or event.id}",
                f"Child run: {event.run_id or '(parent)'}",
                f"Payload: {payload}",
                f"Content:\n{event.content}",
            ]
        )
    text = "\n".join(blocks)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 70)].rstrip() + "\n\n[Thread events truncated to context budget.]"


def _mark_events_consumed(event_ids: Sequence[str]) -> None:
    clean_ids = [str(event_id) for event_id in event_ids if str(event_id)]
    if not clean_ids:
        return
    placeholders = ", ".join("?" for _ in clean_ids)
    conn = _conn()
    try:
        conn.execute(
            f"UPDATE agent_orchestration_messages SET consumed_at = ?, "
            f"delivery_status = 'consumed' WHERE id IN ({placeholders}) "
            "AND consumed_at = ''",
            (_now(), *clean_ids),
        )
        conn.commit()
    finally:
        conn.close()


def _joined_work_pending(orchestration_id: str) -> bool:
    current = [
        member
        for member in list_members(orchestration_id, include_runs=False)
        if member.get("required")
    ]
    return any(
        str(member.get("status") or "") not in TERMINAL_MEMBER_STATUSES
        for member in current
    )


def _joined_terminal_events_unaccounted(orchestration_id: str) -> bool:
    """Close the member-update/event-insert race at the final-answer guard."""

    terminal = [
        member
        for member in list_members(orchestration_id, include_runs=False)
        if member.get("required")
        and str(member.get("status") or "") in TERMINAL_MEMBER_STATUSES
        and str(member.get("status") or "") != "retried"
    ]
    if not terminal:
        return False
    conn = _conn()
    try:
        for member in terminal:
            source_event_id = (
                f"run:{member.get('run_id')}:terminal:{member.get('status')}"
            )
            row = conn.execute(
                "SELECT consumed_at FROM agent_orchestration_messages "
                "WHERE orchestration_id = ? AND source_event_id = ? LIMIT 1",
                (orchestration_id, source_event_id),
            ).fetchone()
            if row is None or not str(row["consumed_at"] or ""):
                return True
    finally:
        conn.close()
    return False


def _completion_status(orchestration_id: str) -> str:
    members = list_members(orchestration_id, include_runs=False)
    failed = any(
        str(member.get("status") or "") in FAILED_MEMBER_STATUSES
        for member in members
        if str(member.get("status") or "") != "retried"
    )
    return "completed_partial" if failed else "completed"


def _checkpoint_output_metadata(parent_thread_id: str, text: str) -> dict[str, str]:
    """Identify the exact parent-authored checkpoint row for an output."""

    try:
        from row_bot.threads import (
            get_latest_checkpoint_messages,
            get_latest_checkpoint_revision,
        )

        checkpoint_message_id = ""
        for message in reversed(get_latest_checkpoint_messages(parent_thread_id)):
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
            if str(content or "").strip() != str(text or "").strip():
                continue
            checkpoint_message_id = str(getattr(message, "id", "") or "").strip()
            break
        return {
            "checkpoint_message_id": checkpoint_message_id,
            "checkpoint_revision": get_latest_checkpoint_revision(parent_thread_id),
        }
    except Exception:
        logger.debug(
            "Could not identify parent checkpoint output for thread %s",
            parent_thread_id,
            exc_info=True,
        )
        return {"checkpoint_message_id": "", "checkpoint_revision": ""}


def complete_parent_pass(
    orchestration_id: str,
    output: str | Mapping[str, Any],
    *,
    continuation_state: Mapping[str, Any] | None = None,
    delivery_context: Mapping[str, Any] | None = None,
    foreground: bool,
    consumed_event_ids: Sequence[str] | None = None,
) -> ParentPassResult:
    """Classify one pass of the original parent graph as progress or final."""

    orchestration = get_orchestration(orchestration_id)
    if not _is_unified_parent(orchestration):
        raise OrchestrationError("Parent passes require a version 2 orchestration.")
    if consumed_event_ids:
        _mark_events_consumed(consumed_event_ids)
    state = dict(orchestration.get("continuation_state_json") or {})
    state.update(dict(continuation_state or {}))
    state["finalization_ready"] = True
    delivery = dict(orchestration.get("delivery_context_json") or {})
    delivery.update(dict(delivery_context or {}))
    if isinstance(output, Mapping):
        output_type = str(output.get("type") or "")
        if output_type == "interrupt":
            _persist_parent_approval(
                orchestration,
                output,
                continuation_state=state,
                delivery_context=delivery,
            )
            return ParentPassResult(
                orchestration_id=orchestration_id,
                parent_state="waiting_approval",
                output_kind="approval",
                waiting=True,
                text="",
            )
        text = str(output.get("message") or output.get("error") or "").strip()
    else:
        text = str(output or "").strip()
    state.pop("parent_approval", None)
    state.pop("parent_interrupt", None)
    if not text:
        raise OrchestrationError("The original parent returned no output.")

    waiting = (
        _joined_work_pending(orchestration_id)
        or _joined_terminal_events_unaccounted(orchestration_id)
        or bool(pending_thread_events(orchestration_id))
    )
    current = get_orchestration(orchestration_id) or orchestration
    attempt = int(current.get("parent_attempt") or 0)
    output_kind = "progress" if waiting else "final"
    parent_state = "waiting" if waiting else "completed"
    status = "waiting_children" if waiting else _completion_status(orchestration_id)
    message_kind = f"parent_{output_kind}"
    message_id = f"orchestration:{orchestration_id}:{message_kind}:{attempt}"
    checkpoint_metadata = _checkpoint_output_metadata(
        str(orchestration.get("parent_thread_id") or ""),
        text,
    )
    record_message(
        orchestration_id,
        kind=message_kind,
        content=text,
        message_id=message_id,
        delivery_status="delivered" if foreground else "pending",
        payload={
            "foreground": bool(foreground),
            "parent_attempt": attempt,
            **checkpoint_metadata,
        },
    )
    now = _now()
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET status = ?, parent_state = ?, "
            "continuation_state_json = ?, delivery_context_json = ?, "
            "wake_requested_at = ?, completed_at = ?, error_message = '', "
            "updated_at = ? WHERE id = ?",
            (
                status,
                parent_state,
                _json_text(state),
                _json_text(delivery),
                now if waiting and pending_thread_events(orchestration_id) else "",
                "" if waiting else now,
                now,
                orchestration_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    final_row = get_orchestration(orchestration_id) or current
    _emit_orchestration_buddy_event(final_row, terminal=not waiting)
    try:
        from row_bot.threads import touch_thread

        touch_thread(str(final_row.get("parent_thread_id") or ""))
    except Exception:
        logger.debug("Could not touch parent thread after checkpoint pass", exc_info=True)
    if not foreground:
        _deliver_once(
            final_row,
            kind=output_kind,
            text=text,
            message_key=message_id,
        )
    if waiting and _parent_wake_ready(orchestration_id):
        request_parent_wake(orchestration_id)
    if not waiting:
        _notify_surface_completion(final_row, text)
    return ParentPassResult(
        orchestration_id=orchestration_id,
        parent_state=parent_state,
        output_kind=output_kind,
        waiting=waiting,
        text=text,
    )


def arm_parent_wait(
    orchestration_id: str,
    *,
    continuation_state: Mapping[str, Any],
    delivery_context: Mapping[str, Any] | None = None,
) -> bool:
    """Arm a structured workflow delegation that has no initial model pass."""

    orchestration = get_orchestration(orchestration_id)
    if not _is_unified_parent(orchestration):
        return False
    state = dict(orchestration.get("continuation_state_json") or {})
    incoming_state = copy.deepcopy(dict(continuation_state or {}))
    if incoming_state.get("workflow_direct_child"):
        workflow_config = incoming_state.get("config")
        if isinstance(workflow_config, dict):
            workflow_config.setdefault("configurable", {})[
                "thread_event_new_turn"
            ] = True
    state.update(incoming_state)
    state["finalization_ready"] = True
    delivery = dict(orchestration.get("delivery_context_json") or {})
    delivery.update(dict(delivery_context or {}))
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'waiting_children', "
            "parent_state = 'waiting', continuation_state_json = ?, "
            "delivery_context_json = ?, updated_at = ? WHERE id = ?",
            (_json_text(state), _json_text(delivery), _now(), orchestration_id),
        )
        conn.commit()
    finally:
        conn.close()
    if _parent_wake_ready(orchestration_id):
        request_parent_wake(orchestration_id)
    return True


def start_parent_event_turn(
    *,
    parent_thread_id: str,
    parent_generation_id: str,
    root_objective: str,
    model_ref: str,
    approval_mode: str,
    runtime_surface: str,
    event_kind: str,
    event_content: str,
    source_event_id: str,
    config: Mapping[str, Any],
    enabled_tool_names: Sequence[str],
    delivery_context: Mapping[str, Any] | None = None,
    parent_run_id: str = "",
    event_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create and wake a v2 parent turn from a durable non-user event."""

    orchestration = create_or_get_orchestration(
        parent_thread_id=parent_thread_id,
        parent_generation_id=parent_generation_id,
        parent_run_id=parent_run_id,
        root_objective=root_objective,
        model_ref=model_ref,
        approval_mode=approval_mode,
        runtime_surface=runtime_surface,
        delivery_context=delivery_context,
        orchestration_version=CURRENT_ORCHESTRATION_VERSION,
    )
    parent_config = copy.deepcopy(dict(config))
    parent_config.setdefault("configurable", {})["thread_event_new_turn"] = True
    arm_parent_wait(
        str(orchestration["id"]),
        continuation_state={
            "config": parent_config,
            "enabled_tool_names": [
                str(name) for name in enabled_tool_names if str(name or "").strip()
            ],
        },
        delivery_context=delivery_context,
    )
    record_thread_event(
        str(orchestration["id"]),
        kind=event_kind,
        content=event_content,
        source_event_id=source_event_id,
        payload=event_payload,
    )
    return get_orchestration(str(orchestration["id"])) or orchestration


def request_parent_wake(orchestration_id: str) -> bool:
    """Request a bounded original-parent pass without holding a provider call."""

    orchestration = get_orchestration(orchestration_id)
    if not _is_unified_parent(orchestration):
        return False
    if str(orchestration.get("status") or "") in {
        "completed",
        "completed_partial",
        "failed",
        "stopped",
        "interrupted",
    }:
        return False
    if not _parent_wake_ready(orchestration_id):
        return True
    parent_state = str(orchestration.get("parent_state") or "")
    now = _now()
    conn = _conn()
    try:
        if parent_state in {"waiting", "runnable"}:
            conn.execute(
                "UPDATE agent_orchestrations SET parent_state = 'runnable', "
                "wake_requested_at = ?, updated_at = ? WHERE id = ? "
                "AND parent_state IN ('waiting', 'runnable')",
                (now, now, orchestration_id),
            )
        else:
            conn.execute(
                "UPDATE agent_orchestrations SET wake_requested_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, orchestration_id),
            )
        conn.commit()
    finally:
        conn.close()
    if parent_state in {"waiting", "runnable"}:
        _schedule_parent_runner(orchestration_id)
    return True


def route_parent_steering(
    *,
    parent_thread_id: str,
    incoming_generation_id: str,
    content: str,
) -> dict[str, Any] | None:
    """Route a later user message into the waiting original parent turn."""

    text = str(content or "").strip()
    if not parent_thread_id or not incoming_generation_id or not text:
        return None
    orchestration = get_active_orchestration(parent_thread_id)
    if (
        not _is_unified_parent(orchestration)
        or str(orchestration.get("status") or "") != "waiting_children"
        or str(orchestration.get("parent_state") or "")
        not in {"waiting", "runnable", "running"}
        or str(orchestration.get("parent_generation_id") or "")
        == str(incoming_generation_id)
    ):
        return None
    record_thread_event(
        str(orchestration["id"]),
        kind="parent_steering",
        content=text,
        source_event_id=f"steering:{incoming_generation_id}",
        payload={"incoming_generation_id": str(incoming_generation_id)},
    )
    return get_orchestration(str(orchestration["id"])) or orchestration


def _claim_parent_lease(orchestration_id: str) -> tuple[dict[str, Any], str] | None:
    lease_owner = uuid.uuid4().hex
    now = _now()
    expires = (datetime.now() + timedelta(minutes=2)).isoformat()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM agent_orchestrations WHERE id = ?",
            (orchestration_id,),
        ).fetchone()
        orchestration = _orchestration_row(row)
        pending_event = conn.execute(
            "SELECT 1 FROM agent_orchestration_messages "
            "WHERE orchestration_id = ? AND kind LIKE 'event.%' "
            "AND consumed_at = '' LIMIT 1",
            (orchestration_id,),
        ).fetchone()
        if (
            not _is_unified_parent(orchestration)
            or str(orchestration.get("parent_state") or "") not in {"waiting", "runnable"}
            or str(orchestration.get("status") or "") in {
                "completed",
                "completed_partial",
                "failed",
                "stopped",
                "interrupted",
                "waiting_approval",
            }
            or pending_event is None
        ):
            conn.commit()
            return None
        existing_owner = str(orchestration.get("lease_owner") or "")
        existing_expiry = str(orchestration.get("lease_expires_at") or "")
        if existing_owner and existing_expiry and existing_expiry > now:
            conn.commit()
            return None
        changed = conn.execute(
            "UPDATE agent_orchestrations SET parent_state = 'running', "
            "lease_owner = ?, lease_expires_at = ?, wake_requested_at = '', "
            "parent_attempt = parent_attempt + 1, updated_at = ? "
            "WHERE id = ? AND parent_state IN ('waiting', 'runnable')",
            (lease_owner, expires, now, orchestration_id),
        ).rowcount
        row = conn.execute(
            "SELECT * FROM agent_orchestrations WHERE id = ?",
            (orchestration_id,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not changed:
        return None
    claimed = _orchestration_row(row)
    return (claimed, lease_owner) if claimed else None


def _release_parent_lease(orchestration_id: str, lease_owner: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET lease_owner = '', lease_expires_at = '', "
            "updated_at = ? WHERE id = ? AND lease_owner = ?",
            (_now(), orchestration_id, lease_owner),
        )
        conn.commit()
    finally:
        conn.close()


def _bind_recorded_parent_resources(
    orchestration: Mapping[str, Any],
    config: dict[str, Any],
) -> None:
    """Restore exact thread resources before a background parent wake."""

    thread_id = str(orchestration.get("parent_thread_id") or "")
    configurable = config.setdefault("configurable", {})
    saved_developer_workspace_id = str(
        configurable.get("developer_workspace_id") or ""
    )
    saved_project_workspace_id = str(
        configurable.get("project_workspace_id") or ""
    )
    saved_designer_project_id = str(configurable.get("designer_project_id") or "")
    try:
        from row_bot.threads import (
            _get_thread_developer_workspace,
            _get_thread_project_id,
            _get_thread_project_workspace,
        )

        developer_workspace_id = _get_thread_developer_workspace(thread_id)
        project_workspace_id = _get_thread_project_workspace(thread_id)
        project_id = _get_thread_project_id(thread_id)
    except Exception:
        developer_workspace_id = ""
        project_workspace_id = ""
        project_id = ""
    for label, saved, durable in (
        ("Developer workspace", saved_developer_workspace_id, developer_workspace_id),
        ("Developer project workspace", saved_project_workspace_id, project_workspace_id),
        ("Designer project", saved_designer_project_id, project_id),
    ):
        if saved and durable and saved != durable:
            raise OrchestrationError(
                f"{label} binding changed while the parent Agent was waiting. "
                "Resume from the originally bound thread or start a new turn."
            )
    effective_developer_workspace_id = (
        saved_developer_workspace_id or developer_workspace_id
    )
    effective_project_workspace_id = (
        saved_project_workspace_id or project_workspace_id
    )
    effective_designer_project_id = saved_designer_project_id or project_id
    if effective_developer_workspace_id or effective_project_workspace_id:
        from row_bot.developer.storage import get_workspace

        for label, workspace_id in (
            ("Developer workspace", effective_developer_workspace_id),
            ("Developer project workspace", effective_project_workspace_id),
        ):
            if workspace_id and get_workspace(workspace_id) is None:
                raise OrchestrationError(
                    f"{label} {workspace_id} no longer exists."
                )
    if effective_developer_workspace_id:
        configurable["developer_workspace_id"] = effective_developer_workspace_id
    if effective_project_workspace_id:
        configurable["project_workspace_id"] = effective_project_workspace_id
    if effective_designer_project_id:
        configurable["designer_project_id"] = effective_designer_project_id
        try:
            from row_bot.designer.session import bind_project_to_thread

            bind_project_to_thread(thread_id, effective_designer_project_id)
        except ImportError:
            pass


def _default_parent_executor(
    orchestration: dict[str, Any],
    event_context: str,
    enabled_tools: list[str],
    config: dict[str, Any],
) -> str | dict[str, Any]:
    from row_bot.agent import invoke_agent

    configurable = config.setdefault("configurable", {})
    configurable.update(
        {
            "thread_id": str(orchestration["parent_thread_id"]),
            "generation_id": str(orchestration["parent_generation_id"]),
            "root_objective": str(orchestration["root_objective"]),
            "model_override": str(orchestration["model_ref"]),
            "approval_mode": str(orchestration["approval_mode"]),
            "runtime_surface": str(orchestration.get("runtime_surface") or "normal_chat"),
            "orchestration_id": str(orchestration["id"]),
            "thread_event_context": True,
            "orchestration_internal_wake": True,
        }
    )
    _bind_recorded_parent_resources(orchestration, config)
    return invoke_agent(event_context, enabled_tools, config)


def _interrupt_fingerprint(interrupts: object) -> str:
    """Return a compact ID-independent fingerprint for protected actions."""

    items = interrupts if isinstance(interrupts, list) else [interrupts]
    actions: list[str] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        args = raw.get("args")
        action = {
            "tool": str(raw.get("tool") or raw.get("name") or ""),
            "args": args if isinstance(args, Mapping) else args or {},
        }
        actions.append(
            json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)
        )
    canonical = "[" + ",".join(sorted(actions)) + "]"
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:24]


def _approval_payload_from_db_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return _parse_object(row.get("approval_payload_json"))


def _persist_parent_approval(
    orchestration: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    continuation_state: Mapping[str, Any] | None = None,
    delivery_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit one parent approval and its orchestration wait state together."""

    interrupts = result.get("interrupts") or []
    if not isinstance(interrupts, list):
        interrupts = []
    fingerprint = _interrupt_fingerprint(interrupts)
    try:
        from row_bot.approval_messages import compact_message, normalize_interrupts

        payload = normalize_interrupts(
            interrupts,
            source_label="Parent Agent",
            parent_thread_id=str(orchestration.get("parent_thread_id") or ""),
        )
        payload["orchestration_id"] = str(orchestration.get("id") or "")
        payload["interrupt_fingerprint"] = fingerprint
        message = compact_message(payload) or "The parent Agent needs approval to continue."
    except Exception:
        payload = {
            "interrupts": interrupts,
            "source_label": "Parent Agent",
            "orchestration_id": str(orchestration.get("id") or ""),
            "interrupt_fingerprint": fingerprint,
        }
        message = "The parent Agent needs approval to continue."

    orchestration_id = str(orchestration.get("id") or "")
    parent_thread_id = str(orchestration.get("parent_thread_id") or "")
    step_id = f"orchestration:{orchestration_id}"
    created = False
    approval: dict[str, Any] = {}
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT continuation_state_json, delivery_context_json "
            "FROM agent_orchestrations WHERE id = ?",
            (orchestration_id,),
        ).fetchone()
        if current is None:
            raise OrchestrationError("Orchestration not found while creating approval.")
        continuation = (
            dict(continuation_state)
            if continuation_state is not None
            else _parse_object(current["continuation_state_json"])
        )
        delivery = (
            dict(delivery_context)
            if delivery_context is not None
            else _parse_object(current["delivery_context_json"])
        )
        existing = continuation.get("parent_approval")
        existing_row = None
        if isinstance(existing, Mapping) and existing.get("approval_id"):
            existing_row = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?",
                (str(existing.get("approval_id") or ""),),
            ).fetchone()
        if existing_row is None:
            existing_row = conn.execute(
                "SELECT * FROM approval_requests WHERE resume_kind = 'parent_orchestration' "
                "AND step_id = ? AND status = 'pending' ORDER BY requested_at DESC LIMIT 1",
                (step_id,),
            ).fetchone()
        if existing_row is not None:
            existing_data = dict(existing_row)
            existing_payload = _approval_payload_from_db_row(existing_data)
            existing_fingerprint = str(
                (existing or {}).get("interrupt_fingerprint")
                if isinstance(existing, Mapping)
                else ""
            ) or str(existing_payload.get("interrupt_fingerprint") or "")
            if not existing_fingerprint:
                existing_interrupts = (
                    (existing or {}).get("interrupts")
                    if isinstance(existing, Mapping)
                    else existing_payload.get("interrupts")
                )
                existing_fingerprint = _interrupt_fingerprint(existing_interrupts)
            if existing_fingerprint == fingerprint:
                approval = {
                    "approval_id": str(existing_data.get("id") or ""),
                    "resume_token": str(existing_data.get("resume_token") or ""),
                    "interrupts": interrupts,
                    "interrupt_fingerprint": fingerprint,
                }
                if str(existing_data.get("status") or "") == "pending":
                    continuation["parent_approval"] = approval
                elif str(existing_data.get("status") or "") in {"approved", "denied"}:
                    approval.update(
                        {
                            "resolved": True,
                            "approved": str(existing_data.get("status") or "") == "approved",
                        }
                    )
                    continuation["parent_approval"] = approval
                else:
                    approval = {}
            elif str(existing_data.get("status") or "") == "pending":
                conn.execute(
                    "UPDATE approval_requests SET status = 'cancelled', "
                    "responded_at = ?, response_note = ? WHERE id = ? "
                    "AND status = 'pending'",
                    (
                        _now(),
                        "Superseded by a different parent checkpoint interrupt.",
                        str(existing_data.get("id") or ""),
                    ),
                )
            if not approval:
                continuation.pop("parent_approval", None)

        if not approval:
            approval_id = uuid.uuid4().hex[:12]
            resume_token = uuid.uuid4().hex
            requested_at = _now()
            timeout_at = (datetime.now() + timedelta(minutes=30)).isoformat()
            conn.execute(
                "INSERT INTO approval_requests "
                "(id, run_id, task_id, step_id, resume_token, message, channel, "
                "status, requested_at, timeout_at, agent_run_id, resume_kind, "
                "source_label, source_thread_id, parent_thread_id, approval_payload_json) "
                "VALUES (?, ?, '', ?, ?, ?, NULL, 'pending', ?, ?, '', "
                "'parent_orchestration', 'Parent Agent', ?, ?, ?)",
                (
                    approval_id,
                    str(orchestration.get("parent_run_id") or ""),
                    step_id,
                    resume_token,
                    message,
                    requested_at,
                    timeout_at,
                    parent_thread_id,
                    parent_thread_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            approval = {
                "approval_id": approval_id,
                "resume_token": resume_token,
                "interrupts": interrupts,
                "interrupt_fingerprint": fingerprint,
            }
            continuation["parent_approval"] = approval
            created = True
        continuation["parent_interrupt"] = dict(result)
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'waiting_approval', "
            "parent_state = 'waiting_approval', continuation_state_json = ?, "
            "delivery_context_json = ?, wake_requested_at = '', updated_at = ? "
            "WHERE id = ?",
            (
                _json_text(continuation),
                _json_text(delivery),
                _now(),
                orchestration_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if created:
        try:
            from row_bot.tasks import _emit_buddy_approval_event

            _emit_buddy_approval_event(
                "needed",
                run_id=str(orchestration.get("parent_run_id") or ""),
                step_id=step_id,
                approval_id=str(approval.get("approval_id") or ""),
                resume_token=str(approval.get("resume_token") or ""),
                label="Approval pending",
                message=message,
            )
        except Exception:
            logger.debug("Could not publish parent approval Buddy event", exc_info=True)
    try:
        from row_bot.channels.thread_notifications import notify_agent_run_approval

        if created:
            notify_agent_run_approval(str(approval.get("approval_id") or ""))
    except Exception:
        logger.debug("Could not publish parent approval", exc_info=True)
    return approval


def _schedule_parent_runner(orchestration_id: str) -> None:
    with _SERVICE_LOCK:
        existing = _PARENT_THREADS.get(orchestration_id)
        if existing is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=_run_parent_thread,
            args=(orchestration_id,),
            daemon=True,
            name=f"orchestration-parent-{orchestration_id}",
        )
        _PARENT_THREADS[orchestration_id] = thread
        thread.start()


def _run_parent_thread(orchestration_id: str) -> None:
    lease_owner = ""
    try:
        claim = _claim_parent_lease(orchestration_id)
        if claim is None:
            return
        orchestration, lease_owner = claim
        events = pending_thread_events(orchestration_id)
        if not events:
            conn = _conn()
            try:
                conn.execute(
                    "UPDATE agent_orchestrations SET parent_state = 'waiting', "
                    "updated_at = ? WHERE id = ? AND lease_owner = ?",
                    (_now(), orchestration_id, lease_owner),
                )
                conn.commit()
            finally:
                conn.close()
            return
        continuation = orchestration.get("continuation_state_json") or {}
        saved_config = continuation.get("config")
        config = copy.deepcopy(saved_config) if isinstance(saved_config, dict) else {
            "configurable": {}
        }
        starts_new_turn = bool(
            (config.get("configurable") or {}).get("thread_event_new_turn")
        )
        if starts_new_turn:
            # Persist that the first pass has started before issuing provider
            # work. A crash or provider error then resumes any checkpointed
            # budget instead of resetting the logical turn.
            retry_config = copy.deepcopy(config)
            retry_config.setdefault("configurable", {}).pop(
                "thread_event_new_turn",
                None,
            )
            retry_continuation = {
                **continuation,
                "config": retry_config,
            }
            conn = _conn()
            try:
                conn.execute(
                    "UPDATE agent_orchestrations SET continuation_state_json = ?, "
                    "updated_at = ? WHERE id = ? AND lease_owner = ?",
                    (
                        _json_text(retry_continuation),
                        _now(),
                        orchestration_id,
                        lease_owner,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        config.setdefault("configurable", {})["thread_event_messages"] = [
            {
                "role": "human" if event.kind == "parent_steering" else "assistant",
                "content": (
                    event.content
                    if event.kind == "parent_steering"
                    else _format_thread_events(orchestration, [event])
                ),
                "source_event_id": event.source_event_id,
            }
            for event in events
        ]
        enabled_tools = [
            str(name)
            for name in continuation.get("enabled_tool_names") or []
            if str(name or "").strip()
        ]
        context = _format_thread_events(orchestration, events)
        executor = _PARENT_EXECUTOR or _default_parent_executor
        output = executor(orchestration, context, enabled_tools, config)
        # Event-only Goal/workflow starts establish a fresh logical turn once.
        # Later waves must resume the budget checkpointed by that first pass.
        config.setdefault("configurable", {}).pop("thread_event_new_turn", None)
        pass_result = complete_parent_pass(
            orchestration_id,
            output,
            continuation_state={
                **continuation,
                "config": config,
                "enabled_tool_names": enabled_tools,
            },
            foreground=False,
            consumed_event_ids=[event.id for event in events],
        )
        if pass_result.output_kind == "approval" and isinstance(output, Mapping):
            current = get_orchestration(orchestration_id) or orchestration
            approval = _persist_parent_approval(current, output)
            if approval.get("resolved"):
                if lease_owner:
                    _release_parent_lease(orchestration_id, lease_owner)
                    lease_owner = ""
                resume_parent_orchestration(
                    orchestration_id,
                    resume_token=str(approval.get("resume_token") or ""),
                    approved=bool(approval.get("approved")),
                )
    except Exception as exc:
        logger.exception("Original parent wake failed for %s", orchestration_id)
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_orchestrations SET parent_state = 'waiting', "
                "error_message = ?, updated_at = ? WHERE id = ?",
                (str(exc), _now(), orchestration_id),
            )
            conn.commit()
        finally:
            conn.close()
    finally:
        if lease_owner:
            _release_parent_lease(orchestration_id, lease_owner)
        with _SERVICE_LOCK:
            _PARENT_THREADS.pop(orchestration_id, None)
        current = get_orchestration(orchestration_id)
        if (
            _is_unified_parent(current)
            and str(current.get("parent_state") or "") == "runnable"
            and _parent_wake_ready(orchestration_id)
        ):
            _schedule_parent_runner(orchestration_id)


def wait_for_parent(
    orchestration_id: str,
    timeout: float = 5.0,
    *,
    terminal_only: bool = True,
    minimum_attempts: int = 0,
) -> dict[str, Any] | None:
    """Bounded test/CLI wait; correctness does not depend on polling."""

    import time

    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() <= deadline:
        current = get_orchestration(orchestration_id)
        if not current:
            return None
        terminal = str(current.get("status") or "") in {
            "completed",
            "completed_partial",
            "failed",
            "stopped",
            "interrupted",
        }
        attempted = int(current.get("parent_attempt") or 0) >= int(minimum_attempts or 0)
        if attempted and (terminal or not terminal_only):
            with _SERVICE_LOCK:
                thread = _PARENT_THREADS.get(orchestration_id)
            if thread is None or not thread.is_alive():
                return current
        time.sleep(0.01)
    return get_orchestration(orchestration_id)


def _mark_parent_resume_interrupted(
    orchestration_id: str,
    continuation: Mapping[str, Any],
    exc: BaseException,
) -> None:
    clean = dict(continuation)
    clean.pop("parent_approval", None)
    clean.pop("parent_interrupt", None)
    message = f"Resume is required after the approval continuation failed: {exc}"
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'interrupted', "
            "parent_state = 'interrupted', continuation_state_json = ?, "
            "lease_owner = '', lease_expires_at = '', wake_requested_at = '', "
            "error_message = ?, updated_at = ? WHERE id = ?",
            (_json_text(clean), message[:1000], _now(), orchestration_id),
        )
        conn.commit()
    finally:
        conn.close()


def resume_parent_orchestration(
    orchestration_id: str,
    *,
    resume_token: str,
    approved: bool,
) -> dict[str, Any] | None:
    """Resume a background interrupt in the original parent checkpoint."""

    orchestration = get_orchestration(orchestration_id)
    if not _is_unified_parent(orchestration):
        return None
    if str(orchestration.get("parent_state") or "") != "waiting_approval":
        return orchestration
    continuation = dict(orchestration.get("continuation_state_json") or {})
    approval = continuation.get("parent_approval")
    if not isinstance(approval, dict) or str(approval.get("resume_token") or "") != str(
        resume_token or ""
    ):
        raise OrchestrationError("The parent approval resume token does not match.")
    config_value = continuation.get("config")
    config = copy.deepcopy(config_value) if isinstance(config_value, dict) else {
        "configurable": {}
    }
    enabled_tools = [
        str(name)
        for name in continuation.get("enabled_tool_names") or []
        if str(name or "").strip()
    ]
    configurable = config.setdefault("configurable", {})
    configurable.update(
        {
            "thread_id": str(orchestration["parent_thread_id"]),
            "generation_id": str(orchestration["parent_generation_id"]),
            "root_objective": str(orchestration["root_objective"]),
            "model_override": str(orchestration["model_ref"]),
            "approval_mode": str(orchestration["approval_mode"]),
            "runtime_surface": str(orchestration.get("runtime_surface") or "normal_chat"),
            "orchestration_id": str(orchestration_id),
            "orchestration_internal_wake": True,
        }
    )
    approval_fingerprint = str(approval.get("interrupt_fingerprint") or "")
    if not approval_fingerprint:
        approval_fingerprint = _interrupt_fingerprint(approval.get("interrupts") or [])
    current_interrupts: list[dict[str, Any]] = []
    interrupt_ids: list[str] | None = None
    if _PARENT_EXECUTOR is None:
        try:
            from row_bot.agent import get_invoke_agent_interrupts

            current_interrupts = get_invoke_agent_interrupts(enabled_tools, config)
            if not current_interrupts:
                raise OrchestrationError(
                    "The saved parent checkpoint no longer contains an approval interrupt."
                )
            current_fingerprint = _interrupt_fingerprint(current_interrupts)
            if current_fingerprint != approval_fingerprint:
                _persist_parent_approval(
                    orchestration,
                    {"type": "interrupt", "interrupts": current_interrupts},
                )
                return get_orchestration(orchestration_id)
            interrupt_ids = [
                str(item.get("__interrupt_id") or "")
                for item in current_interrupts
                if str(item.get("__interrupt_id") or "")
            ]
            if len(interrupt_ids) != len(current_interrupts):
                raise OrchestrationError(
                    "The saved parent checkpoint has an incomplete interrupt group."
                )
        except Exception as exc:
            _mark_parent_resume_interrupted(orchestration_id, continuation, exc)
            raise

    clean_continuation = dict(continuation)
    clean_continuation.pop("parent_approval", None)
    clean_continuation.pop("parent_interrupt", None)
    lease_owner = uuid.uuid4().hex
    expires = (datetime.now() + timedelta(minutes=2)).isoformat()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE agent_orchestrations SET status = 'running', "
            "parent_state = 'running', lease_owner = ?, lease_expires_at = ?, "
            "parent_attempt = parent_attempt + 1, continuation_state_json = ?, "
            "wake_requested_at = '', error_message = '', updated_at = ? "
            "WHERE id = ? AND parent_state = 'waiting_approval'",
            (
                lease_owner,
                expires,
                _json_text(clean_continuation),
                _now(),
                orchestration_id,
            ),
        ).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not changed:
        return get_orchestration(orchestration_id)
    try:
        _bind_recorded_parent_resources(orchestration, config)
        if _PARENT_EXECUTOR is not None:
            event_context = (
                "Message Type: PARENT_APPROVAL_RESOLVED\n"
                f"Approved: {str(bool(approved)).lower()}\n"
                "Continue the original parent turn naturally."
            )
            result = _PARENT_EXECUTOR(
                get_orchestration(orchestration_id) or orchestration,
                event_context,
                enabled_tools,
                config,
            )
        else:
            from row_bot.agent import resume_invoke_agent

            result = resume_invoke_agent(
                enabled_tools,
                config,
                approved,
                interrupt_ids=interrupt_ids,
            )
        pass_result = complete_parent_pass(
            orchestration_id,
            result,
            continuation_state={
                **clean_continuation,
                "config": config,
                "enabled_tool_names": enabled_tools,
            },
            foreground=False,
        )
        if pass_result.output_kind == "approval" and isinstance(result, Mapping):
            _persist_parent_approval(
                get_orchestration(orchestration_id) or orchestration,
                result,
            )
    except Exception as exc:
        _mark_parent_resume_interrupted(
            orchestration_id,
            clean_continuation,
            exc,
        )
        raise
    finally:
        _release_parent_lease(orchestration_id, lease_owner)
    return get_orchestration(orchestration_id)


def message_orchestration(
    orchestration_id: str,
    content: str,
    *,
    run_id: str = "",
) -> int:
    """Queue guidance for one live child or every live current member."""

    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        raise OrchestrationError("Orchestration not found.")
    targets = [
        member
        for member in list_members(orchestration_id, include_runs=False)
        if member.get("status") not in TERMINAL_MEMBER_STATUSES
        and (not run_id or member.get("run_id") == run_id)
    ]
    from row_bot.agent_runs import append_agent_parent_message

    delivered = 0
    for member in targets:
        target_run_id = str(member["run_id"])
        if append_agent_parent_message(target_run_id, content):
            delivered += 1
            record_message(
                orchestration_id,
                kind="steering",
                content=content,
                run_id=target_run_id,
                delivery_status="pending",
            )
    return delivered


def mark_steering_delivered(run_id: str, contents: Sequence[str]) -> None:
    """Acknowledge guidance after a child consumes it at a safe boundary."""

    clean = {str(content or "").strip() for content in contents if str(content or "").strip()}
    if not clean:
        return
    member = get_member_for_run(run_id)
    if not member:
        return
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, content FROM agent_orchestration_messages "
            "WHERE orchestration_id = ? AND run_id = ? AND kind = 'steering' "
            "AND delivery_status = 'pending'",
            (member["orchestration_id"], str(run_id)),
        ).fetchall()
        ids = [str(row["id"]) for row in rows if str(row["content"]).strip() in clean]
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"UPDATE agent_orchestration_messages SET delivery_status = 'delivered', "
                f"delivered_at = ? WHERE id IN ({placeholders})",
                (_now(), *ids),
            )
            conn.commit()
    finally:
        conn.close()


def pending_steering_for_run(run_id: str) -> list[str]:
    member = get_member_for_run(run_id)
    if not member:
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT content FROM agent_orchestration_messages "
            "WHERE orchestration_id = ? AND run_id = ? AND kind = 'steering' "
            "AND delivery_status = 'pending' ORDER BY created_at, id",
            (member["orchestration_id"], str(run_id)),
        ).fetchall()
    finally:
        conn.close()
    return [str(row["content"]) for row in rows if str(row["content"] or "").strip()]


def _barrier_ready(orchestration_id: str) -> bool:
    orchestration = get_orchestration(orchestration_id)
    if not orchestration or int(orchestration.get("required_total") or 0) <= 0:
        return False
    required = [
        member
        for member in list_members(orchestration_id, include_runs=False)
        if member.get("required")
    ]
    return (
        len(required) == int(orchestration["required_total"])
        and all(member["status"] in TERMINAL_MEMBER_STATUSES for member in required)
    )


def is_transient_failure(run: Mapping[str, Any] | None) -> bool:
    if not run:
        return False
    status = str(run.get("status") or "").lower()
    if status == "timed_out":
        return True
    if status not in {"failed", "blocked"}:
        return False
    text = " ".join(
        str(run.get(name) or "")
        for name in ("error", "status_message", "terminal_reason", "summary")
    ).lower()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _default_retry_executor(
    orchestration: dict[str, Any],
    member: dict[str, Any],
    explicit_resume: bool,
) -> dict[str, Any]:
    from row_bot import agent_runner
    from row_bot.agent_runs import get_agent_run

    original = get_agent_run(str(member["run_id"])) or {}
    developer_workspace_id = str(original.get("workspace_id") or "")
    if str(original.get("workspace_mode") or "") == "worktree":
        try:
            from row_bot.developer.worktrees import get_worktree_for_run

            worktree = get_worktree_for_run(str(member["run_id"])) or {}
            developer_workspace_id = str(
                worktree.get("project_workspace_id") or developer_workspace_id
            )
        except Exception:
            pass
    return agent_runner.spawn_agent_run(
        str(original.get("prompt") or ""),
        parent_thread_id=str(original.get("parent_thread_id") or ""),
        parent_run_id=str(original.get("parent_run_id") or ""),
        parent_message_id=str(original.get("parent_message_id") or ""),
        profile=str(original.get("profile_id") or original.get("profile_slug") or "worker"),
        display_name=str(original.get("display_name") or ""),
        context=str(original.get("context_summary") or ""),
        context_mode=str(original.get("context_mode") or "focused"),
        enabled_tool_names=list(original.get("tools_override") or []),
        model_override=str(original.get("model_override") or orchestration.get("model_ref") or ""),
        approval_mode=str(original.get("approval_mode") or orchestration.get("approval_mode") or ""),
        developer_workspace_id=developer_workspace_id,
        workspace_mode=str(original.get("workspace_mode") or ""),
        use_worktree=str(original.get("workspace_mode") or "") == "worktree",
        orchestration_id=str(orchestration["id"]),
        orchestration_required=bool(member.get("required")),
        orchestration_dependencies=list(member.get("dependency_run_ids_json") or []),
        orchestration_attempt=int(member.get("attempt") or 1) + (0 if explicit_resume else 1),
        retry_of_run_id=str(member["run_id"]),
        wait=False,
    )


def retry_member(
    run_id: str,
    *,
    explicit_resume: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Create one auditable replacement run while retaining the failed attempt."""

    member = get_member_for_run(run_id)
    if not member:
        raise OrchestrationError("The Agent Run is not an orchestration member.")
    orchestration = get_orchestration(str(member["orchestration_id"]))
    if not orchestration:
        raise OrchestrationError("Orchestration not found.")
    from row_bot.agent_runs import get_agent_run

    run = get_agent_run(run_id) or {}
    if str(run.get("status") or "") not in TERMINAL_MEMBER_STATUSES | {"interrupted"}:
        raise OrchestrationError("Only a terminal or interrupted Agent Run can be retried.")
    if not force and not is_transient_failure(run):
        raise OrchestrationError("This failure is not safe to retry automatically.")
    if not explicit_resume and int(member.get("attempt") or 1) >= 2:
        raise OrchestrationError("The transient retry has already been used.")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestration_members SET status = 'retrying' "
            "WHERE orchestration_id = ? AND run_id = ?",
            (member["orchestration_id"], run_id),
        )
        conn.commit()
    finally:
        conn.close()
    executor = _RETRY_EXECUTOR or _default_retry_executor
    try:
        replacement = executor(orchestration, member, explicit_resume)
    except Exception:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_orchestration_members SET status = ? "
                "WHERE orchestration_id = ? AND run_id = ?",
                (
                    str(run.get("status") or "failed"),
                    member["orchestration_id"],
                    run_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        raise
    return replacement


def _ordered_result_packet(orchestration: dict[str, Any], limit: int = 24000) -> str:
    lines = [
        "Continue the suspended parent turn and give one consolidated final answer.",
        "Do not delegate the already completed work again.",
        f"Original objective: {orchestration.get('root_objective', '')}",
        "",
        "Ordered child results:",
    ]
    members = list_members(str(orchestration["id"]))
    for member in members:
        if not member.get("required"):
            continue
        run = member.get("run") or {}
        if str(member.get("status")) == "retried":
            continue
        summary = str(
            run.get("summary")
            or run.get("error")
            or run.get("status_message")
            or "No result was returned."
        ).strip()
        workspace = str(run.get("workspace_path") or "").strip()
        lines.append(
            f"\n[{member['sequence'] + 1}] {run.get('display_name') or 'Agent'} "
            f"({member.get('status')}, profile={run.get('profile_slug') or 'worker'})"
        )
        lines.append(f"Objective: {run.get('prompt') or ''}")
        lines.append(f"Result: {summary}")
        if workspace:
            lines.append(f"Unintegrated worktree/artifact workspace: {workspace}")
    optional = [
        member
        for member in members
        if not member.get("required") and member.get("status") != "retried"
    ]
    if optional:
        lines.extend(["", "Optional background work (does not block this answer):"])
        for member in optional:
            run = member.get("run") or {}
            status = str(member.get("status") or run.get("status") or "unknown")
            summary = str(
                run.get("summary")
                or run.get("error")
                or run.get("status_message")
                or "No result available yet."
            ).strip()
            lines.append(
                f"- {run.get('display_name') or 'Agent'}: {status}; {summary}"
            )
    steering = list_messages(str(orchestration["id"]), kinds=["steering"])
    if steering:
        lines.extend(["", "User/parent steering received while waiting:"])
        lines.extend(f"- {message['content']}" for message in steering)
    lines.extend(
        [
            "",
            "Reconcile disagreements and disclose required failures or unintegrated "
            "worktree results. Answer the original user directly.",
        ]
    )
    packet = "\n".join(lines)
    if len(packet) <= limit:
        return packet
    return packet[: max(0, limit - 80)].rstrip() + "\n\n[Result packet truncated to context budget.]"


def _default_synthesis_executor(
    orchestration: dict[str, Any],
    prompt: str,
) -> str:
    from row_bot.agent import invoke_agent

    continuation = orchestration.get("continuation_state_json") or {}
    saved_config = continuation.get("config")
    config = copy.deepcopy(saved_config) if isinstance(saved_config, dict) else {
        "configurable": {}
    }
    configurable = config.setdefault("configurable", {})
    synthesis_thread_id = f"orchestration-synthesis:{orchestration['id']}"
    configurable["thread_id"] = synthesis_thread_id
    configurable["generation_id"] = (
        f"{orchestration.get('parent_generation_id') or orchestration['id']}:synthesis"
    )
    configurable["model_override"] = orchestration["model_ref"]
    configurable["approval_mode"] = orchestration["approval_mode"]
    configurable["orchestration_id"] = orchestration["id"]
    configurable["orchestration_continuation"] = True
    enabled_tools = [
        str(name)
        for name in (continuation.get("enabled_tool_names") or [])
        if str(name) != "agents"
    ]
    try:
        result = invoke_agent(prompt, list(enabled_tools), config)
    finally:
        try:
            from row_bot.thread_cleanup import delete_threads

            delete_threads([synthesis_thread_id])
        except Exception:
            logger.debug(
                "Could not clean up synthesis thread %s",
                synthesis_thread_id,
                exc_info=True,
            )
    if isinstance(result, dict):
        if result.get("type") == "interrupt":
            raise OrchestrationError(
                "Parent synthesis needs approval; resume it from the orchestration controls."
            )
        if result.get("type") in {"error", "terminal"}:
            raise OrchestrationError(
                str(result.get("error") or result.get("message") or "Synthesis failed.")
            )
    return str(result or "").strip()


def _default_delivery_executor(
    orchestration: dict[str, Any],
    kind: str,
    text: str,
    key: str,
) -> bool:
    from row_bot.channels.thread_notifications import deliver_parent_thread_notification

    return deliver_parent_thread_notification(
        key=key,
        thread_id=str(orchestration["parent_thread_id"]),
        kind=f"orchestration_{kind}",
        text=text,
        ui_metadata={
            "orchestration_id": orchestration["id"],
            "orchestration_message_kind": kind,
        },
        payload={
            "orchestration_id": orchestration["id"],
            "runtime_surface": orchestration.get("runtime_surface", ""),
        },
        checkpoint_authoritative=(
            _is_unified_parent(orchestration) and kind in {"progress", "final"}
        ),
    )


def _deliver_once(
    orchestration: dict[str, Any],
    *,
    kind: str,
    text: str,
    message_key: str = "",
) -> bool:
    with _DELIVERY_LOCK:
        key = str(message_key or f"orchestration:{orchestration['id']}:{kind}")
        existing = record_message(
            str(orchestration["id"]),
            kind=f"parent_{kind}" if message_key else kind,
            content=text,
            message_id=key,
        )
        if str(existing.get("delivery_status") or "") == "delivered":
            return True
        executor = _DELIVERY_EXECUTOR or _default_delivery_executor
        delivered = False
        delivery_error = ""
        try:
            delivered = bool(executor(orchestration, kind, text, key))
            if not delivered:
                delivery_error = "Delivery executor returned false."
        except Exception as exc:
            delivery_error = str(exc)
            logger.exception("Orchestration %s delivery failed", key)
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_orchestration_messages SET delivery_status = ?, "
                "delivered_at = ?, attempt_count = attempt_count + 1, "
                "last_error = ? WHERE id = ?",
                (
                    "delivered" if delivered else "failed",
                    _now() if delivered else "",
                    "" if delivered else delivery_error[:1000],
                    key,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return delivered


def ensure_acknowledgement(orchestration_id: str) -> bool:
    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        return False
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT acknowledgement_sent, required_total "
            "FROM agent_orchestrations WHERE id = ?",
            (orchestration_id,),
        ).fetchone()
        if row is None or int(row["acknowledgement_sent"] or 0):
            conn.commit()
            return bool(row and row["acknowledgement_sent"])
        conn.execute(
            "UPDATE agent_orchestrations SET acknowledgement_sent = 1, "
            "updated_at = ? WHERE id = ?",
            (_now(), orchestration_id),
        )
        conn.commit()
        count = int(row["required_total"] or 0)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    text = (
        "I'm working on this with 1 agent."
        if count == 1
        else f"I'm working on this with {count} agents."
    )
    return _deliver_once(orchestration, kind="acknowledgement", text=text)


def finalize_parent_generation(
    orchestration_id: str,
    *,
    continuation_state: Mapping[str, Any],
    delivery_context: Mapping[str, Any] | None = None,
) -> bool:
    """Suspend a finalizing parent generation when required work exists."""

    orchestration = get_orchestration(orchestration_id)
    if not orchestration or int(orchestration.get("required_total") or 0) <= 0:
        return False
    if _is_unified_parent(orchestration):
        state = dict(continuation_state or {})
        output = str(state.pop("parent_output", "") or "").strip()
        if not output:
            try:
                from row_bot.threads import get_latest_checkpoint_messages

                for message in reversed(
                    get_latest_checkpoint_messages(
                        str(orchestration.get("parent_thread_id") or "")
                    )
                ):
                    if str(getattr(message, "type", "") or "") != "ai":
                        continue
                    content = getattr(message, "content", "")
                    output = str(content if isinstance(content, str) else "")
                    if output.strip():
                        break
            except Exception:
                logger.debug("Could not recover the parent pass output", exc_info=True)
        if not output:
            raise OrchestrationError(
                "The original parent output is required for unified orchestration."
            )
        result = complete_parent_pass(
            orchestration_id,
            output,
            continuation_state=state,
            delivery_context=delivery_context,
            foreground=True,
        )
        return result.waiting
    if orchestration["status"] in {
        "completed",
        "completed_partial",
        "failed",
        "stopped",
        "interrupted",
    }:
        return orchestration["status"] == "interrupted"
    state = dict(continuation_state or {})
    state["finalization_ready"] = True
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'waiting_children', "
            "continuation_state_json = ?, delivery_context_json = ?, "
            "updated_at = ? WHERE id = ? AND status NOT IN "
            "('completed', 'completed_partial', 'failed', 'stopped', 'interrupted')",
            (
                _json_text(state),
                _json_text(dict(delivery_context or orchestration.get("delivery_context_json") or {})),
                _now(),
                orchestration_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    ensure_acknowledgement(orchestration_id)
    _claim_and_schedule_synthesis(orchestration_id)
    return True


def _claim_and_schedule_synthesis(orchestration_id: str) -> bool:
    if not _barrier_ready(orchestration_id):
        return False
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, continuation_state_json FROM agent_orchestrations WHERE id = ?",
            (orchestration_id,),
        ).fetchone()
        state = _parse_object(row["continuation_state_json"]) if row else {}
        if (
            row is None
            or str(row["status"] or "") != "waiting_children"
            or not state.get("finalization_ready")
        ):
            conn.commit()
            return False
        changed = conn.execute(
            "UPDATE agent_orchestrations SET status = 'synthesizing', updated_at = ? "
            "WHERE id = ? AND status = 'waiting_children'",
            (_now(), orchestration_id),
        ).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not changed:
        return False
    _schedule_synthesis(orchestration_id)
    return True


def _schedule_synthesis(orchestration_id: str) -> None:
    with _SERVICE_LOCK:
        existing = _SYNTHESIS_THREADS.get(orchestration_id)
        if existing is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=_run_synthesis,
            args=(orchestration_id,),
            daemon=True,
            name=f"orchestration-synthesis-{orchestration_id}",
        )
        _SYNTHESIS_THREADS[orchestration_id] = thread
        thread.start()


def _run_synthesis(orchestration_id: str) -> None:
    try:
        orchestration = get_orchestration(orchestration_id)
        if not orchestration or orchestration["status"] != "synthesizing":
            return
        prompt = _ordered_result_packet(orchestration)
        continuation = orchestration.get("continuation_state_json") or {}
        if continuation.get("workflow_direct_child"):
            summaries: list[str] = []
            for member in list_members(orchestration_id):
                if not member.get("required") or member.get("status") == "retried":
                    continue
                run = member.get("run") or {}
                summaries.append(
                    str(
                        run.get("summary")
                        or run.get("error")
                        or run.get("status_message")
                        or f"Agent {run.get('id') or ''} returned no summary."
                    )
                )
            text = "\n\n".join(summary for summary in summaries if summary.strip())
        else:
            executor = _SYNTHESIS_EXECUTOR or _default_synthesis_executor
            text = str(executor(orchestration, prompt) or "").strip()
        if not text:
            raise OrchestrationError("Parent synthesis returned no final answer.")
        members = list_members(orchestration_id, include_runs=False)
        required_members = [member for member in members if member.get("required")]
        terminal_optional_failures = any(
            not member.get("required")
            and member["status"] in FAILED_MEMBER_STATUSES
            for member in members
        )
        final_status = (
            "completed_partial"
            if (
                any(
                    member["status"] in FAILED_MEMBER_STATUSES
                    for member in required_members
                )
                or terminal_optional_failures
            )
            else "completed"
        )
        conn = _conn()
        try:
            changed = conn.execute(
                "UPDATE agent_orchestrations SET status = ?, completed_at = ?, "
                "updated_at = ?, error_message = '' WHERE id = ? "
                "AND status = 'synthesizing'",
                (final_status, _now(), _now(), orchestration_id),
            ).rowcount
            conn.commit()
        finally:
            conn.close()
        if changed:
            final = get_orchestration(orchestration_id) or orchestration
            _emit_orchestration_buddy_event(final, terminal=True)
            _deliver_once(final, kind="final", text=text)
            _notify_surface_completion(final, text)
    except Exception as exc:
        logger.exception("Orchestration synthesis failed for %s", orchestration_id)
        conn = _conn()
        try:
            changed = conn.execute(
                "UPDATE agent_orchestrations SET status = 'failed', "
                "error_message = ?, completed_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'synthesizing'",
                (str(exc), _now(), _now(), orchestration_id),
            ).rowcount
            conn.commit()
        finally:
            conn.close()
        if changed:
            orchestration = get_orchestration(orchestration_id)
            if orchestration:
                _emit_orchestration_buddy_event(orchestration, terminal=True)
                _deliver_once(
                    orchestration,
                    kind="final",
                    text=f"I couldn't complete the delegated synthesis: {exc}",
                )
    finally:
        with _SERVICE_LOCK:
            _SYNTHESIS_THREADS.pop(orchestration_id, None)


def _notify_surface_completion(orchestration: Mapping[str, Any], text: str) -> None:
    try:
        from row_bot.goals import after_orchestration_completion

        after_orchestration_completion(
            str(orchestration.get("parent_thread_id") or ""),
            str(orchestration.get("id") or ""),
            text,
        )
    except (ImportError, AttributeError):
        pass
    except Exception:
        logger.exception("Goal continuation failed after orchestration completion")
    try:
        from row_bot.tasks import resume_workflows_waiting_for_orchestration

        resume_workflows_waiting_for_orchestration(str(orchestration.get("id") or ""))
    except (ImportError, AttributeError):
        pass
    except Exception:
        logger.exception("Workflow continuation failed after orchestration completion")


def handle_run_terminal(run_or_id: Mapping[str, Any] | str) -> bool:
    """React to a terminal event; return whether legacy notification is owned."""

    from row_bot.agent_runs import get_agent_run

    run = (
        dict(run_or_id)
        if isinstance(run_or_id, Mapping)
        else get_agent_run(str(run_or_id)) or {}
    )
    run_id = str(run.get("id") or "")
    member = get_member_for_run(run_id)
    if not member:
        return False
    owns_completion_delivery = bool(member.get("required"))
    orchestration_id = str(member["orchestration_id"])
    status = str(run.get("status") or "")
    if status not in TERMINAL_MEMBER_STATUSES:
        return owns_completion_delivery
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestration_members SET status = ? "
            "WHERE orchestration_id = ? AND run_id = ?",
            (status, orchestration_id, run_id),
        )
        conn.execute(
            "UPDATE agent_orchestrations SET updated_at = ? WHERE id = ?",
            (_now(), orchestration_id),
        )
        conn.commit()
    finally:
        conn.close()
    _wake_dependency_waiters(orchestration_id)
    orchestration = get_orchestration(orchestration_id) or {}
    if (
        orchestration.get("status") not in {"stopped", "interrupted"}
        and is_transient_failure(run)
        and int(member.get("attempt") or 1) < 2
    ):
        try:
            replacement = retry_member(run_id)
            if _is_unified_parent(orchestration):
                record_thread_event(
                    orchestration_id,
                    kind="child_retry_scheduled",
                    content=(
                        f"Child {run.get('display_name') or run_id} failed transiently; "
                        f"replacement {replacement.get('id') or ''} was scheduled."
                    ),
                    run_id=run_id,
                    source_event_id=f"run:{run_id}:retry:{replacement.get('id') or ''}",
                    payload={
                        "failed_run_id": run_id,
                        "replacement_run_id": str(replacement.get("id") or ""),
                        "status": status,
                        "error": str(run.get("error") or run.get("status_message") or ""),
                    },
                )
            # Neither required nor optional callers should see an intermediate
            # completion notification when a durable replacement is active.
            return True
        except Exception as exc:
            logger.warning("Automatic child retry failed for %s: %s", run_id, exc)
    if _is_unified_parent(orchestration):
        record_thread_event(
            orchestration_id,
            kind="child_terminal",
            content=str(
                run.get("summary")
                or run.get("error")
                or run.get("status_message")
                or f"Child {run_id} finished with status {status}."
            ),
            run_id=run_id,
            source_event_id=f"run:{run_id}:terminal:{status}",
            payload={
                "run_id": run_id,
                "display_name": str(run.get("display_name") or ""),
                "status": status,
                "summary": str(run.get("summary") or ""),
                "error": str(run.get("error") or ""),
                "terminal_reason": str(run.get("terminal_reason") or ""),
                "workspace_path": str(run.get("workspace_path") or ""),
                "required": bool(member.get("required")),
                "attempt": int(member.get("attempt") or 1),
            },
        )
        return owns_completion_delivery
    if orchestration.get("status") == "waiting_approval":
        continuation = orchestration.get("continuation_state_json") or {}
        next_status = "waiting_children" if continuation.get("finalization_ready") else "running"
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_orchestrations SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval'",
                (next_status, _now(), orchestration_id),
            )
            conn.commit()
        finally:
            conn.close()
    if int(orchestration.get("required_total") or 0) > 0:
        _claim_and_schedule_synthesis(orchestration_id)
    else:
        members = list_members(orchestration_id, include_runs=False)
        if members and all(
            str(row.get("status") or "") in TERMINAL_MEMBER_STATUSES
            for row in members
        ):
            failures = any(
                str(row.get("status") or "") != "completed" for row in members
            )
            conn = _conn()
            try:
                conn.execute(
                    "UPDATE agent_orchestrations SET status = ?, completed_at = ?, "
                    "updated_at = ? WHERE id = ? AND status NOT IN "
                    "('completed', 'completed_partial', 'stopped')",
                    (
                        "completed_partial" if failures else "completed",
                        _now(),
                        _now(),
                        orchestration_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    return owns_completion_delivery


def handle_run_status(run_id: str, status: str) -> bool:
    """Mirror non-terminal approval/queue state into an owned orchestration."""

    member = get_member_for_run(run_id)
    if not member:
        return False
    orchestration_id = str(member["orchestration_id"])
    status = str(status or "")
    orchestration = get_orchestration(orchestration_id)
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestration_members SET status = ? "
            "WHERE orchestration_id = ? AND run_id = ?",
            (status, orchestration_id, str(run_id)),
        )
        if status == "waiting_approval" and not _is_unified_parent(orchestration):
            conn.execute(
                "UPDATE agent_orchestrations SET status = 'waiting_approval', "
                "updated_at = ? WHERE id = ? AND status NOT IN "
                "('completed', 'completed_partial', 'failed', 'stopped', 'interrupted')",
                (_now(), orchestration_id),
            )
        elif status in {"queued", "running"} and not _is_unified_parent(orchestration):
            row = conn.execute(
                "SELECT continuation_state_json FROM agent_orchestrations WHERE id = ?",
                (orchestration_id,),
            ).fetchone()
            continuation = _parse_object(row["continuation_state_json"]) if row else {}
            target = "waiting_children" if continuation.get("finalization_ready") else "running"
            conn.execute(
                "UPDATE agent_orchestrations SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval'",
                (target, _now(), orchestration_id),
            )
        conn.commit()
    finally:
        conn.close()
    if status == "waiting_approval" and _is_unified_parent(orchestration):
        record_thread_event(
            orchestration_id,
            kind="child_approval_requested",
            content=f"Child {run_id} is waiting for approval.",
            run_id=run_id,
            source_event_id=f"run:{run_id}:approval:requested",
            payload={"run_id": run_id, "status": status},
        )
    return True


def stop_orchestration(orchestration_id: str, *, run_id: str = "") -> dict[str, Any]:
    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        raise OrchestrationError("Orchestration not found.")
    from row_bot import agent_runner
    unified = _is_unified_parent(orchestration)

    if run_id:
        member = get_member_for_run(run_id)
        if not member or member["orchestration_id"] != orchestration_id:
            raise OrchestrationError("The Agent Run is not in this orchestration.")
        if unified:
            record_thread_event(
                orchestration_id,
                kind="stop_requested",
                content=f"Stop requested for child {run_id}.",
                run_id=run_id,
                source_event_id=f"stop:{run_id}",
                payload={"scope": "child", "run_id": run_id},
            )
        else:
            record_message(
                orchestration_id,
                kind="stop",
                content="Stop requested",
                run_id=run_id,
                delivery_status="delivered",
            )
        agent_runner.stop_agent_run(run_id)
        return orchestration_overview(orchestration_id)
    if unified:
        record_thread_event(
            orchestration_id,
            kind="stop_requested",
            content="The user requested that all joined child work stop.",
            source_event_id=f"stop-all:{orchestration_id}",
            payload={"scope": "all"},
        )
        for member in list_members(orchestration_id, include_runs=False):
            if member["status"] not in TERMINAL_MEMBER_STATUSES:
                agent_runner.stop_agent_run(str(member["run_id"]))
        return orchestration_overview(orchestration_id)
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'stopped', completed_at = ?, "
            "updated_at = ? WHERE id = ? AND status NOT IN "
            "('completed', 'completed_partial', 'failed', 'stopped')",
            (_now(), _now(), orchestration_id),
        )
        conn.commit()
    finally:
        conn.close()
    terminal_row = get_orchestration(orchestration_id)
    if terminal_row:
        _emit_orchestration_buddy_event(terminal_row, terminal=True)
    record_message(
        orchestration_id,
        kind="stop",
        content="Stop all requested",
        delivery_status="delivered",
    )
    for member in list_members(orchestration_id, include_runs=False):
        if member["status"] not in TERMINAL_MEMBER_STATUSES:
            agent_runner.stop_agent_run(str(member["run_id"]))
    return orchestration_overview(orchestration_id)


_TERMINAL_EVENT_STATUS = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.stopped": "stopped",
    "run.blocked": "blocked",
}


def _latest_recorded_terminal_status(
    conn: Any,
    run_id: str,
) -> tuple[str, str]:
    row = conn.execute(
        "SELECT type, ts FROM agent_run_events WHERE run_id = ? "
        "AND type IN ('run.completed', 'run.failed', 'run.stopped', 'run.blocked') "
        "ORDER BY ts DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return "", ""
    return _TERMINAL_EVENT_STATUS.get(str(row["type"] or ""), ""), str(
        row["ts"] or ""
    )


def repair_interrupted_orchestrations_batch(
    *,
    limit: int = 20,
    after_id: str = "",
) -> dict[str, Any]:
    """Repair one bounded restart batch using only local durable records."""

    _ensure_schema()
    statuses = sorted(ACTIVE_ORCHESTRATION_STATUSES | {"interrupted"})
    placeholders = ", ".join("?" for _ in statuses)
    batch_limit = max(1, min(100, int(limit or 20)))
    cursor = str(after_id or "")
    now = _now()
    processed = 0
    interrupted_members = 0
    restored_runs = 0
    next_cursor = cursor
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT * FROM agent_orchestrations WHERE status IN ({placeholders}) "
            "AND id > ? ORDER BY id LIMIT ?",
            (*statuses, cursor, batch_limit),
        ).fetchall()
        for orchestration_row in rows:
            orchestration = dict(orchestration_row)
            orchestration_id = str(orchestration.get("id") or "")
            next_cursor = orchestration_id
            processed += 1
            member_rows = conn.execute(
                "SELECT m.run_id, m.status AS member_status, r.status AS run_status "
                "FROM agent_orchestration_members m "
                "LEFT JOIN agent_runs r ON r.id = m.run_id "
                "WHERE m.orchestration_id = ?",
                (orchestration_id,),
            ).fetchall()
            parent_thread_id = str(orchestration.get("parent_thread_id") or "")
            pending_rows = conn.execute(
                "SELECT agent_run_id, step_id FROM approval_requests "
                "WHERE status = 'pending' AND parent_thread_id = ?",
                (parent_thread_id,),
            ).fetchall()
            member_run_ids = {
                str(row["run_id"] or "")
                for row in member_rows
                if str(row["run_id"] or "")
            }
            pending_run_ids = {
                str(row["agent_run_id"] or "")
                for row in pending_rows
                if str(row["agent_run_id"] or "") in member_run_ids
            }
            parent_approval_pending = any(
                str(row["step_id"] or "") == f"orchestration:{orchestration_id}"
                for row in pending_rows
            )
            for member_row in member_rows:
                member = dict(member_row)
                run_id = str(member.get("run_id") or "")
                member_status = str(member.get("member_status") or "")
                run_status = str(member.get("run_status") or member_status)
                if member_status in {"retried", "transferred", "cleared"}:
                    continue
                desired_status = run_status
                terminal_ts = ""
                if run_status in TERMINAL_MEMBER_STATUSES:
                    desired_status = run_status
                elif run_status == "interrupted":
                    recorded_status, terminal_ts = _latest_recorded_terminal_status(
                        conn,
                        run_id,
                    )
                    desired_status = recorded_status or "interrupted"
                elif run_status == "waiting_approval" and run_id in pending_run_ids:
                    desired_status = "waiting_approval"
                else:
                    desired_status = "interrupted"

                if desired_status in TERMINAL_MEMBER_STATUSES and run_status not in TERMINAL_MEMBER_STATUSES:
                    restored_runs += conn.execute(
                        "UPDATE agent_runs SET status = ?, "
                        "status_message = CASE WHEN status_message = '' "
                        "THEN 'Recovered from the latest terminal event' ELSE status_message END, "
                        "finished_at = CASE WHEN finished_at = '' THEN ? ELSE finished_at END, "
                        "heartbeat_at = '', updated_at = ? WHERE id = ? "
                        f"AND status NOT IN ({', '.join('?' for _ in TERMINAL_MEMBER_STATUSES)})",
                        (
                            desired_status,
                            terminal_ts or now,
                            now,
                            run_id,
                            *sorted(TERMINAL_MEMBER_STATUSES),
                        ),
                    ).rowcount
                elif desired_status == "interrupted" and run_status not in TERMINAL_MEMBER_STATUSES:
                    conn.execute(
                        "UPDATE agent_runs SET status = 'interrupted', "
                        "status_message = 'App restarted; Resume is required', "
                        "heartbeat_at = '', updated_at = ? WHERE id = ? "
                        f"AND status NOT IN ({', '.join('?' for _ in TERMINAL_MEMBER_STATUSES)})",
                        (now, run_id, *sorted(TERMINAL_MEMBER_STATUSES)),
                    )
                if member_status != desired_status:
                    conn.execute(
                        "UPDATE agent_orchestration_members SET status = ? "
                        "WHERE orchestration_id = ? AND run_id = ?",
                        (desired_status, orchestration_id, run_id),
                    )
                    if desired_status == "interrupted":
                        interrupted_members += 1

            has_pending_approval = parent_approval_pending or bool(pending_run_ids)
            if has_pending_approval:
                conn.execute(
                    "UPDATE agent_orchestrations SET lease_owner = '', "
                    "lease_expires_at = '', wake_requested_at = '', updated_at = ? "
                    "WHERE id = ?",
                    (now, orchestration_id),
                )
            else:
                conn.execute(
                    "UPDATE agent_orchestrations SET status = 'interrupted', "
                    "parent_state = CASE WHEN orchestration_version >= 2 "
                    "THEN 'interrupted' ELSE parent_state END, "
                    "lease_owner = '', lease_expires_at = '', wake_requested_at = '', "
                    "error_message = 'App restarted; Resume is required', updated_at = ? "
                    "WHERE id = ?",
                    (now, orchestration_id),
                )
        has_more = False
        if rows:
            has_more = conn.execute(
                f"SELECT 1 FROM agent_orchestrations WHERE status IN ({placeholders}) "
                "AND id > ? LIMIT 1",
                (*statuses, next_cursor),
            ).fetchone() is not None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "processed": processed,
        "orchestrations_interrupted": processed,
        "members_interrupted": interrupted_members,
        "runs_restored": restored_runs,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def recover_interrupted_orchestrations() -> dict[str, int]:
    """Compatibility helper that drains the bounded local repair batches."""

    totals = {
        "orchestrations_interrupted": 0,
        "members_interrupted": 0,
    }
    cursor = ""
    while True:
        result = repair_interrupted_orchestrations_batch(
            limit=50,
            after_id=cursor,
        )
        totals["orchestrations_interrupted"] += int(result.get("processed") or 0)
        totals["members_interrupted"] += int(
            result.get("members_interrupted") or 0
        )
        if not result.get("has_more"):
            return totals
        cursor = str(result.get("next_cursor") or cursor)


def _validate_resume(orchestration: Mapping[str, Any]) -> None:
    from row_bot.tools import registry

    if not registry.is_enabled("agents"):
        raise OrchestrationError("Agents are disabled; enable the Agents tool before Resume.")
    model_ref = str(orchestration.get("model_ref") or "")
    if not model_ref:
        raise OrchestrationError("The saved parent model is missing.")
    try:
        from row_bot.providers.readiness import ensure_agent_ready

        ensure_agent_ready(model_ref)
    except Exception as exc:
        raise OrchestrationError(str(exc)) from exc
    continuation = orchestration.get("continuation_state_json") or {}
    saved_config = continuation.get("config")
    config = copy.deepcopy(saved_config) if isinstance(saved_config, dict) else {
        "configurable": {}
    }
    _bind_recorded_parent_resources(orchestration, config)
    delivery = orchestration.get("delivery_context_json") or {}
    runtime_surface = str(
        delivery.get("runtime_surface")
        or orchestration.get("runtime_surface")
        or ""
    )
    if runtime_surface == "channel":
        from row_bot.tasks import get_thread_channel_ref

        channel_ref = get_thread_channel_ref(
            str(orchestration.get("parent_thread_id") or "")
        )
        if not channel_ref:
            raise OrchestrationError(
                "The saved channel target no longer exists for this parent thread."
            )
        saved_channel = str(delivery.get("runtime_channel") or "")
        if saved_channel and str(channel_ref.get("channel") or "") != saved_channel:
            raise OrchestrationError(
                "The saved channel binding changed; refusing to resume to a new target."
            )
    for member in list_members(str(orchestration["id"])):
        if member["status"] != "interrupted":
            continue
        run = member.get("run") or {}
        workspace_path = str(run.get("workspace_path") or "")
        if workspace_path and not Path(workspace_path).exists():
            raise OrchestrationError(
                f"The saved child workspace no longer exists: {workspace_path}"
            )


def _repair_interrupted_parent_checkpoint(
    orchestration: Mapping[str, Any],
) -> None:
    """Close abandoned tool calls before the original parent runs again."""

    if not _is_unified_parent(orchestration):
        return
    continuation = orchestration.get("continuation_state_json") or {}
    saved_config = continuation.get("config")
    config = copy.deepcopy(saved_config) if isinstance(saved_config, dict) else {
        "configurable": {}
    }
    configurable = config.setdefault("configurable", {})
    configurable.update({
        "thread_id": str(orchestration.get("parent_thread_id") or ""),
        "generation_id": str(orchestration.get("parent_generation_id") or ""),
        "root_objective": str(orchestration.get("root_objective") or ""),
        "model_override": str(orchestration.get("model_ref") or ""),
        "approval_mode": str(orchestration.get("approval_mode") or ""),
        "runtime_surface": str(
            orchestration.get("runtime_surface") or "normal_chat"
        ),
    })
    _bind_recorded_parent_resources(orchestration, config)
    enabled_tools = [
        str(name)
        for name in continuation.get("enabled_tool_names") or []
        if str(name or "").strip()
    ]
    from row_bot.agent import repair_orphaned_tool_calls

    repaired = repair_orphaned_tool_calls(
        enabled_tools,
        config,
        orphan_message=(
            "[Interrupted by app restart; this tool call was not retried]"
        ),
        marker_message="\u23f9\ufe0f *[Interrupted by app restart]*",
    )
    if repaired is None:
        raise OrchestrationError(
            "The interrupted parent checkpoint could not be repaired safely."
        )


def resume_orchestration(orchestration_id: str) -> dict[str, Any]:
    """Explicitly resume only unfinished required members after revalidation."""

    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        raise OrchestrationError("Orchestration not found.")
    if orchestration["status"] != "interrupted":
        raise OrchestrationError("Only an interrupted orchestration can be resumed.")
    _validate_resume(orchestration)
    _repair_interrupted_parent_checkpoint(orchestration)
    interrupted = [
        member
        for member in list_members(orchestration_id, include_runs=False)
        if member.get("required") and member.get("status") == "interrupted"
    ]
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'running', "
            "error_message = '', updated_at = ? WHERE id = ? AND status = 'interrupted'",
            (_now(), orchestration_id),
        )
        conn.commit()
    finally:
        conn.close()
    for member in interrupted:
        retry_member(str(member["run_id"]), explicit_resume=True, force=True)
    current = get_orchestration(orchestration_id) or orchestration
    continuation = current.get("continuation_state_json") or {}
    if _is_unified_parent(current):
        wake_ready = _parent_wake_ready(orchestration_id)
        target_status = (
            "waiting_children"
            if continuation.get("finalization_ready") or wake_ready
            else "running"
        )
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_orchestrations SET status = ?, parent_state = 'waiting', "
                "error_message = '', lease_owner = '', lease_expires_at = '', "
                "updated_at = ? WHERE id = ?",
                (target_status, _now(), orchestration_id),
            )
            conn.commit()
        finally:
            conn.close()
        if wake_ready:
            request_parent_wake(orchestration_id)
        return orchestration_overview(orchestration_id)
    if continuation.get("finalization_ready"):
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_orchestrations SET status = 'waiting_children', "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (_now(), orchestration_id),
            )
            conn.commit()
        finally:
            conn.close()
        _claim_and_schedule_synthesis(orchestration_id)
    return orchestration_overview(orchestration_id)


def retry_pending_deliveries(limit: int = 50) -> int:
    _ensure_schema()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_orchestration_messages "
            "WHERE kind IN ('acknowledgement', 'final', 'parent_progress', 'parent_final') "
            "AND delivery_status != 'delivered' ORDER BY created_at LIMIT ?",
            (max(1, int(limit or 50)),),
        ).fetchall()
    finally:
        conn.close()
    delivered = 0
    for row in rows:
        orchestration = get_orchestration(str(row["orchestration_id"]))
        kind = str(row["kind"])
        if orchestration and _deliver_once(
            orchestration,
            kind=kind.removeprefix("parent_"),
            text=str(row["content"]),
            message_key=str(row["id"]) if kind.startswith("parent_") else "",
        ):
            delivered += 1
    return delivered


def wait_for_synthesis(orchestration_id: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """Test/CLI helper; correctness never depends on this wait."""

    with _SERVICE_LOCK:
        thread = _SYNTHESIS_THREADS.get(str(orchestration_id))
    if thread is not None:
        thread.join(timeout=max(0.0, float(timeout)))
    return get_orchestration(orchestration_id)
