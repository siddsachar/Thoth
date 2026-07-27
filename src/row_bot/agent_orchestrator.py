"""Durable orchestration for parent turns that delegate required child work.

The UI may observe these records, but terminal child events and SQLite
transitions coordinate retries, completion barriers, synthesis, and delivery.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
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
_SYNTHESIS_EXECUTOR: Callable[[dict[str, Any], str], str] | None = None
_RETRY_EXECUTOR: Callable[[dict[str, Any], dict[str, Any], bool], dict[str, Any]] | None = None
_DELIVERY_EXECUTOR: Callable[[dict[str, Any], str, str, str], bool] | None = None
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
    for name in ("required_total", "optional_total", "acknowledgement_sent"):
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


def set_test_executors(
    *,
    synthesis: Callable[[dict[str, Any], str], str] | None = None,
    retry: Callable[[dict[str, Any], dict[str, Any], bool], dict[str, Any]] | None = None,
    delivery: Callable[[dict[str, Any], str, str, str], bool] | None = None,
) -> None:
    """Install deterministic service executors; passing no callbacks resets them."""

    global _SYNTHESIS_EXECUTOR, _RETRY_EXECUTOR, _DELIVERY_EXECUTOR
    with _SERVICE_LOCK:
        _SYNTHESIS_EXECUTOR = synthesis
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
    delivery_context: Mapping[str, Any] | None = None,
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
                "settings_snapshot_json, created_at, updated_at, completed_at, "
                "error_message) VALUES (?, ?, ?, ?, ?, 'planning', ?, ?, ?, 0, 0, "
                "0, '{}', ?, ?, ?, ?, '', '')",
                (
                    orchestration_id,
                    parent_thread_id,
                    parent_generation_id,
                    str(parent_run_id or ""),
                    root_objective,
                    model_ref,
                    str(approval_mode or ""),
                    str(runtime_surface or "chat"),
                    _json_text(dict(delivery_context or {})),
                    _json_text(snapshot),
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
    members = list_members(orchestration_id, include_runs=False)
    current_members = [member for member in members if member.get("required")]
    return {
        "running": sum(
            member["status"] in {"queued", "running", "waiting_approval", "interrupted"}
            for member in current_members
        ),
        "completed": sum(member["status"] == "completed" for member in current_members),
        "failed": sum(member["status"] in FAILED_MEMBER_STATUSES for member in current_members),
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
) -> dict[str, Any]:
    _ensure_schema()
    message_id = str(message_id or uuid.uuid4().hex[:12])
    now = _now()
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agent_orchestration_messages "
            "(id, orchestration_id, run_id, kind, content, delivery_status, "
            "created_at, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                str(orchestration_id),
                str(run_id or ""),
                str(kind or "steering"),
                str(content or ""),
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
    return dict(row) if row is not None else {}


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
    return [dict(row) for row in rows]


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
            from row_bot.threads import delete_threads

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
    )


def _deliver_once(
    orchestration: dict[str, Any],
    *,
    kind: str,
    text: str,
) -> bool:
    with _DELIVERY_LOCK:
        key = f"orchestration:{orchestration['id']}:{kind}"
        existing = record_message(
            str(orchestration["id"]),
            kind=kind,
            content=text,
            message_id=key,
        )
        if str(existing.get("delivery_status") or "") == "delivered":
            return True
        executor = _DELIVERY_EXECUTOR or _default_delivery_executor
        delivered = False
        try:
            delivered = bool(executor(orchestration, kind, text, key))
        except Exception:
            logger.exception("Orchestration %s delivery failed", key)
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_orchestration_messages SET delivery_status = ?, "
                "delivered_at = ? WHERE id = ?",
                ("delivered" if delivered else "failed", _now() if delivered else "", key),
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
            retry_member(run_id)
            # Neither required nor optional callers should see an intermediate
            # completion notification when a durable replacement is active.
            return True
        except Exception as exc:
            logger.warning("Automatic child retry failed for %s: %s", run_id, exc)
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
    conn = _conn()
    try:
        conn.execute(
            "UPDATE agent_orchestration_members SET status = ? "
            "WHERE orchestration_id = ? AND run_id = ?",
            (status, orchestration_id, str(run_id)),
        )
        if status == "waiting_approval":
            conn.execute(
                "UPDATE agent_orchestrations SET status = 'waiting_approval', "
                "updated_at = ? WHERE id = ? AND status NOT IN "
                "('completed', 'completed_partial', 'failed', 'stopped', 'interrupted')",
                (_now(), orchestration_id),
            )
        elif status in {"queued", "running"}:
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
    return True


def stop_orchestration(orchestration_id: str, *, run_id: str = "") -> dict[str, Any]:
    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        raise OrchestrationError("Orchestration not found.")
    from row_bot import agent_runner

    if run_id:
        member = get_member_for_run(run_id)
        if not member or member["orchestration_id"] != orchestration_id:
            raise OrchestrationError("The Agent Run is not in this orchestration.")
        record_message(
            orchestration_id,
            kind="stop",
            content="Stop requested",
            run_id=run_id,
            delivery_status="delivered",
        )
        agent_runner.stop_agent_run(run_id)
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


def recover_interrupted_orchestrations() -> dict[str, int]:
    """Mark active orchestration work interrupted without issuing provider calls."""

    _ensure_schema()
    active_statuses = sorted(ACTIVE_ORCHESTRATION_STATUSES)
    placeholders = ", ".join("?" for _ in active_statuses)
    now = _now()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT id FROM agent_orchestrations WHERE status IN ({placeholders})",
            active_statuses,
        ).fetchall()
        orchestration_ids = [str(row["id"]) for row in rows]
        interrupted_members = 0
        if orchestration_ids:
            orch_placeholders = ", ".join("?" for _ in orchestration_ids)
            interrupted_members = conn.execute(
                f"UPDATE agent_orchestration_members SET status = 'interrupted' "
                f"WHERE orchestration_id IN ({orch_placeholders}) "
                f"AND status NOT IN ({', '.join('?' for _ in TERMINAL_MEMBER_STATUSES)})",
                (*orchestration_ids, *sorted(TERMINAL_MEMBER_STATUSES)),
            ).rowcount
            run_rows = conn.execute(
                f"SELECT run_id FROM agent_orchestration_members "
                f"WHERE orchestration_id IN ({orch_placeholders}) "
                "AND status = 'interrupted'",
                orchestration_ids,
            ).fetchall()
            run_ids = [str(row["run_id"]) for row in run_rows]
            if run_ids:
                run_placeholders = ", ".join("?" for _ in run_ids)
                conn.execute(
                    f"UPDATE agent_runs SET status = 'interrupted', "
                    f"status_message = 'App restarted; Resume is required', "
                    f"heartbeat_at = '', updated_at = ? "
                    f"WHERE id IN ({run_placeholders})",
                    (now, *run_ids),
                )
            conn.execute(
                f"UPDATE agent_orchestrations SET status = 'interrupted', "
                f"error_message = 'App restarted; Resume is required', updated_at = ? "
                f"WHERE id IN ({orch_placeholders})",
                (now, *orchestration_ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "orchestrations_interrupted": len(orchestration_ids),
        "members_interrupted": interrupted_members,
    }


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
    for member in list_members(str(orchestration["id"])):
        if member["status"] != "interrupted":
            continue
        run = member.get("run") or {}
        workspace_path = str(run.get("workspace_path") or "")
        if workspace_path and not Path(workspace_path).exists():
            raise OrchestrationError(
                f"The saved child workspace no longer exists: {workspace_path}"
            )


def resume_orchestration(orchestration_id: str) -> dict[str, Any]:
    """Explicitly resume only unfinished required members after revalidation."""

    orchestration = get_orchestration(orchestration_id)
    if not orchestration:
        raise OrchestrationError("Orchestration not found.")
    if orchestration["status"] != "interrupted":
        raise OrchestrationError("Only an interrupted orchestration can be resumed.")
    _validate_resume(orchestration)
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
            "WHERE kind IN ('acknowledgement', 'final') "
            "AND delivery_status != 'delivered' ORDER BY created_at LIMIT ?",
            (max(1, int(limit or 50)),),
        ).fetchall()
    finally:
        conn.close()
    delivered = 0
    for row in rows:
        orchestration = get_orchestration(str(row["orchestration_id"]))
        if orchestration and _deliver_once(
            orchestration,
            kind=str(row["kind"]),
            text=str(row["content"]),
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
