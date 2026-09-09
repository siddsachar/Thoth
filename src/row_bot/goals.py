"""Durable thread-scoped Goal Mode state and continuation decisions."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from row_bot.agent_runs import (
    DEFAULT_AGENT_SETTINGS,
    append_agent_event,
    create_agent_run,
    ensure_agent_run_schema,
    finish_agent_run,
    get_agent_run,
    update_agent_status,
)

logger = logging.getLogger(__name__)

GOAL_ACTIVE_STATUSES = {"active", "waiting_approval"}
GOAL_VISIBLE_STATUSES = {
    "active",
    "paused",
    "waiting_approval",
    "blocked",
    "completed",
}
GOAL_TERMINAL_STATUSES = {"completed", "cleared", "blocked"}
GOAL_CONTROL_TOKENS = {"pause", "resume", "clear", "done", "status", "show"}
GOAL_VERDICTS = {"continue", "complete", "blocked", "needs_user", "paused"}
DEFAULT_GOAL_MAX_TURNS = int(DEFAULT_AGENT_SETTINGS.get("goal_max_turns", 20) or 20)
_GOAL_STATUS_ORDER = ("active", "waiting_approval", "paused", "blocked", "completed", "cleared")


class GoalError(ValueError):
    """Raised when a Goal Mode request is invalid."""


@dataclass(frozen=True)
class GoalContinuationDecision:
    """Result of post-turn goal evaluation."""

    goal: dict[str, Any] | None
    should_continue: bool
    continuation_prompt: str = ""
    reason: str = ""
    status: str = ""


GoalVerifier = Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


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
            raise GoalError("Goal JSON fields must contain valid JSON.") from exc
        value = parsed
    if list_ok:
        if not isinstance(value, list):
            value = [value]
    elif not isinstance(value, dict):
        raise GoalError("Goal JSON object field must be an object.")
    return json.dumps(value, sort_keys=True)


def _parse_json(value: Any, *, list_ok: bool = False) -> Any:
    if value is None or value == "":
        return [] if list_ok else {}
    if isinstance(value, (dict, list)):
        return json.loads(json.dumps(value))
    try:
        parsed = json.loads(str(value))
    except Exception:
        return [] if list_ok else {}
    if list_ok:
        return parsed if isinstance(parsed, list) else []
    return parsed if isinstance(parsed, dict) else {}


def _normalize_status(status: str, *, default: str = "active") -> str:
    value = str(status or default).strip().lower()
    aliases = {
        "running": "active",
        "in_progress": "active",
        "continue": "active",
        "needs_user": "blocked",
        "done": "completed",
        "complete": "completed",
        "cancelled": "cleared",
        "canceled": "cleared",
    }
    value = aliases.get(value, value)
    if value not in {"active", "paused", "waiting_approval", "blocked", "completed", "cleared"}:
        raise GoalError(f"Invalid goal status: {status}")
    return value


def _normalize_verdict(verdict: str, *, default: str = "continue") -> str:
    value = str(verdict or default).strip().lower()
    aliases = {
        "active": "continue",
        "running": "continue",
        "in_progress": "continue",
        "completed": "complete",
        "done": "complete",
        "waiting_user": "needs_user",
        "waiting_approval": "paused",
    }
    value = aliases.get(value, value)
    return value if value in GOAL_VERDICTS else default


def _coerce_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [stripped]
    return [value]


def _merge_entries(existing: list[Any], new_items: list[Any], *, limit: int = 25) -> list[Any]:
    merged = list(existing or [])
    seen = {json.dumps(item, sort_keys=True, default=str) for item in merged}
    for item in new_items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[-limit:]


def _goal_from_row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for field in ("evidence_json", "subgoals_json", "blockers_json"):
        data[field] = _parse_json(data.get(field), list_ok=True)
    for field in (
        "turns_used",
        "max_turns",
        "token_budget",
        "tokens_used",
        "blocker_count",
        "verifier_failures",
    ):
        try:
            data[field] = int(data.get(field) or 0)
        except (TypeError, ValueError):
            data[field] = 0
    return data


def _ensure_goal_schema() -> None:
    ensure_agent_run_schema()
    conn = _get_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS goal_turn_claims (goal_id TEXT NOT NULL, turn_id TEXT NOT NULL, PRIMARY KEY(goal_id,turn_id))")
        conn.commit()
    finally:
        conn.close()


def get_goal(goal_id: str) -> dict[str, Any] | None:
    _ensure_goal_schema()
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM thread_goals WHERE id = ?", (str(goal_id),)).fetchone()
        return _goal_from_row(row)
    finally:
        conn.close()


def get_current_goal(thread_id: str, *, include_terminal: bool = False) -> dict[str, Any] | None:
    _ensure_goal_schema()
    status_set = GOAL_VISIBLE_STATUSES if include_terminal else (GOAL_VISIBLE_STATUSES - GOAL_TERMINAL_STATUSES)
    statuses = tuple(status for status in _GOAL_STATUS_ORDER if status in status_set)
    return _get_current_goal_for_statuses(thread_id, statuses)


def _get_current_goal_for_statuses(thread_id: str, statuses: tuple[str, ...]) -> dict[str, Any] | None:
    if not statuses:
        return None
    placeholders = ", ".join("?" for _ in statuses)
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT * FROM thread_goals WHERE thread_id = ? AND status IN ({placeholders}) "
            "ORDER BY updated_at DESC LIMIT 1",
            (str(thread_id or ""), *statuses),
        ).fetchone()
        return _goal_from_row(row)
    finally:
        conn.close()


def list_goals(
    *,
    thread_id: str | None = None,
    statuses: list[str] | tuple[str, ...] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    _ensure_goal_schema()
    clauses: list[str] = []
    params: list[Any] = []
    if thread_id:
        clauses.append("thread_id = ?")
        params.append(str(thread_id))
    if statuses:
        clean = [_normalize_status(status) for status in statuses]
        placeholders = ", ".join("?" for _ in clean)
        clauses.append(f"status IN ({placeholders})")
        params.extend(clean)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM thread_goals {where} ORDER BY updated_at DESC LIMIT ?",
            (*params, max(1, int(limit or 20))),
        ).fetchall()
        return [goal for row in rows if (goal := _goal_from_row(row))]
    finally:
        conn.close()


def _active_thread_profile_ref(thread_id: str) -> str:
    try:
        from row_bot.threads import _get_thread_agent_profile

        pointer = _get_thread_agent_profile(thread_id)
        return str(pointer.get("id") or pointer.get("slug") or "")
    except Exception:
        return ""


def start_goal(
    thread_id: str,
    objective: str,
    *,
    max_turns: int | None = None,
    replace: bool = True,
) -> dict[str, Any]:
    """Create or replace the active goal for a thread."""
    _ensure_goal_schema()
    thread_id = str(thread_id or "").strip()
    objective = str(objective or "").strip()
    if not thread_id:
        raise GoalError("A thread id is required to start a goal.")
    if not objective:
        raise GoalError("A goal objective is required.")
    max_turns = max(1, int(max_turns or DEFAULT_GOAL_MAX_TURNS))
    now = _now()
    goal_id = uuid.uuid4().hex[:12]
    if replace:
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE thread_goals SET status = 'cleared', updated_at = ?, "
                "last_reason = 'Replaced by a new goal.' "
                "WHERE thread_id = ? AND status IN ('active', 'paused', 'waiting_approval', 'blocked')",
                (now, thread_id),
            )
            conn.commit()
        finally:
            conn.close()
    run_id = f"goal-{goal_id}"
    profile_ref = _active_thread_profile_ref(thread_id)
    create_agent_run(
        run_id=run_id,
        kind="goal",
        status="running",
        status_message="Goal active",
        parent_thread_id=thread_id,
        thread_id=thread_id,
        goal_id=goal_id,
        profile_id=profile_ref,
        display_name=f"Goal: {_short_objective(objective)}",
        prompt=objective,
        max_turns=max_turns,
        turns_used=0,
    )
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO thread_goals "
            "(id, thread_id, objective, status, created_at, updated_at, turns_used, max_turns, "
            "last_verdict, last_reason, last_progress, evidence_json, subgoals_json, blockers_json, "
            "active_run_id, last_turn_id, continuation_key) "
            "VALUES (?, ?, ?, 'active', ?, ?, 0, ?, 'continue', ?, '', '[]', '[]', '[]', ?, '', '')",
            (
                goal_id,
                thread_id,
                objective,
                now,
                now,
                max_turns,
                "Goal started.",
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    append_agent_event(
        run_id,
        "goal.started",
        {"goal_id": goal_id, "objective": objective, "max_turns": max_turns},
        visibility="parent_summary",
    )
    goal = get_goal(goal_id)
    assert goal is not None
    return goal


def pause_goal(thread_id: str, *, reason: str = "Paused by user.") -> dict[str, Any] | None:
    return _set_current_goal_status(
        thread_id,
        "paused",
        reason=reason,
        verdict="paused",
        source_statuses=("active", "waiting_approval"),
    )


def resume_goal(thread_id: str) -> dict[str, Any] | None:
    goal = _set_current_goal_status(
        thread_id,
        "active",
        reason="Goal resumed.",
        verdict="continue",
        source_statuses=("paused", "blocked", "waiting_approval"),
    )
    if goal and int(goal.get("turns_used") or 0) >= int(goal.get("max_turns") or DEFAULT_GOAL_MAX_TURNS):
        extend_goal_budget(goal["id"])
        goal = get_goal(goal["id"])
    return goal


def clear_goal(thread_id: str, *, reason: str = "Cleared by user.") -> dict[str, Any] | None:
    return _set_current_goal_status(
        thread_id,
        "cleared",
        reason=reason,
        verdict="paused",
        finish_run_status="stopped",
        source_statuses=("active", "paused", "waiting_approval", "blocked", "completed"),
    )


def complete_goal(thread_id: str, *, reason: str = "Marked complete.") -> dict[str, Any] | None:
    return _set_current_goal_status(
        thread_id,
        "completed",
        reason=reason,
        verdict="complete",
        finish_run_status="completed",
        source_statuses=("active", "paused", "waiting_approval", "blocked"),
    )


def block_goal(thread_id: str, *, reason: str = "Goal blocked.") -> dict[str, Any] | None:
    return _set_current_goal_status(
        thread_id,
        "blocked",
        reason=reason,
        verdict="blocked",
        finish_run_status="blocked",
        source_statuses=("active", "paused", "waiting_approval"),
    )


def extend_goal_budget(goal_id: str, *, turns: int | None = None) -> dict[str, Any] | None:
    _ensure_goal_schema()
    turns = max(1, int(turns or DEFAULT_GOAL_MAX_TURNS))
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE thread_goals SET max_turns = COALESCE(max_turns, 0) + ?, updated_at = ? WHERE id = ?",
            (turns, now, str(goal_id)),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    return get_goal(goal_id) if changed else None


def _set_current_goal_status(
    thread_id: str,
    status: str,
    *,
    reason: str = "",
    verdict: str = "",
    finish_run_status: str = "",
    source_statuses: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    goal = (
        _get_current_goal_for_statuses(thread_id, source_statuses)
        if source_statuses is not None
        else get_current_goal(thread_id, include_terminal=True)
    )
    if not goal:
        return None
    return set_goal_status(
        goal["id"],
        status,
        reason=reason,
        verdict=verdict,
        finish_run_status=finish_run_status,
    )


def set_goal_status(
    goal_id: str,
    status: str,
    *,
    reason: str = "",
    verdict: str = "",
    finish_run_status: str = "",
    expected_revision: int | None = None,
) -> dict[str, Any] | None:
    _ensure_goal_schema()
    status = _normalize_status(status)
    verdict = _normalize_verdict(verdict or status)
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE thread_goals SET status = ?, last_verdict = ?, last_reason = ?, updated_at = ?, revision=revision+1 "
            "WHERE id = ? AND (? IS NULL OR revision=?)",
            (status, verdict, str(reason or ""), now, str(goal_id), expected_revision, expected_revision),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    if not changed:
        return None
    goal = get_goal(goal_id)
    if goal:
        _sync_goal_run(goal, finish_run_status=finish_run_status)
    return goal


def update_goal_progress(
    *,
    thread_id: str = "",
    goal_id: str = "",
    status: str = "active",
    progress: str = "",
    evidence: list[Any] | str | None = None,
    blockers: list[Any] | str | None = None,
    next_step: str = "",
    verdict: str = "",
) -> dict[str, Any]:
    """Record structured progress from the Goal tool."""
    _ensure_goal_schema()
    goal = get_goal(goal_id) if goal_id else get_current_goal(thread_id)
    if not goal:
        raise GoalError("No active goal is available for this thread.")
    normalized_status = _normalize_status(status)
    normalized_verdict = _normalize_verdict(verdict or normalized_status)
    evidence_list = _merge_entries(goal.get("evidence_json") or [], _coerce_list(evidence))
    blocker_items = _coerce_list(blockers)
    blockers_list = _merge_entries(goal.get("blockers_json") or [], blocker_items)
    blocker_text = _canonical_blocker(blocker_items[-1] if blocker_items else "")
    previous_blocker = str(goal.get("last_blocker") or "")
    blocker_count = int(goal.get("blocker_count") or 0)
    if blocker_text:
        blocker_count = blocker_count + 1 if blocker_text == previous_blocker else 1
    elif normalized_status in {"completed", "cleared"}:
        blocker_count = 0
    last_reason = _reason_from_update(
        status=normalized_status,
        progress=progress,
        blockers=blocker_items,
        next_step=next_step,
    )
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE thread_goals SET status = ?, updated_at = ?, last_verdict = ?, "
            "last_reason = ?, last_progress = ?, evidence_json = ?, blockers_json = ?, "
            "last_blocker = ?, blocker_count = ?, revision=revision+1 WHERE id = ? AND revision=?",
            (
                normalized_status,
                now,
                normalized_verdict,
                last_reason,
                str(progress or next_step or ""),
                _json_text(evidence_list, list_ok=True),
                _json_text(blockers_list, list_ok=True),
                blocker_text or previous_blocker,
                blocker_count,
                str(goal["id"]),
                int(goal.get("revision") or 0),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    updated = get_goal(goal["id"])
    assert updated is not None
    _sync_goal_run(updated)
    return updated


def _sync_goal_run(goal: Mapping[str, Any], *, finish_run_status: str = "") -> None:
    run_id = str(goal.get("active_run_id") or "")
    if not run_id:
        return
    status = str(goal.get("status") or "")
    message = str(goal.get("last_reason") or goal.get("last_progress") or "")
    if finish_run_status:
        finish_agent_run(
            run_id,
            finish_run_status,
            summary=str(goal.get("last_progress") or ""),
            result_json={"goal_id": goal.get("id"), "status": status},
            error=message if finish_run_status in {"failed", "blocked", "stopped"} else "",
        )
        return
    if get_agent_run(run_id):
        mapped = {
            "active": "running",
            "paused": "paused",
            "waiting_approval": "waiting_approval",
            "blocked": "blocked",
            "completed": "completed",
            "cleared": "stopped",
        }.get(status, "running")
        if mapped in {"blocked", "completed", "stopped"}:
            finish_agent_run(
                run_id,
                mapped,
                summary=str(goal.get("last_progress") or ""),
                result_json={"goal_id": goal.get("id"), "status": status},
                error=message if mapped in {"blocked", "stopped"} else "",
            )
        else:
            update_agent_status(run_id, mapped, message, append_event=False)
        append_agent_event(
            run_id,
            "goal.updated",
            {
                "goal_id": goal.get("id"),
                "status": status,
                "verdict": goal.get("last_verdict"),
                "reason": message,
                "turns_used": goal.get("turns_used", 0),
                "max_turns": goal.get("max_turns", 0),
            },
            visibility="parent_summary",
        )


def handle_goal_command(thread_id: str | None, arg: str = "") -> str:
    """Handle `/goal` status/control/start commands for slash surfaces."""
    if not thread_id:
        return "Could not identify the current conversation thread."
    raw = str(arg or "").strip()
    if not raw or raw.lower() in {"status", "show"}:
        return format_goal_status(thread_id)
    token, _, rest = raw.partition(" ")
    command = token.lower()
    if command == "pause":
        goal = pause_goal(thread_id)
        return _goal_action_response(goal, "Goal paused.", "No active goal to pause.")
    if command == "resume":
        goal = resume_goal(thread_id)
        return _goal_action_response(goal, "Goal resumed.", "No paused goal to resume.")
    if command == "clear":
        goal = clear_goal(thread_id)
        return _goal_action_response(goal, "Goal cleared.", "No goal to clear.")
    if command == "done":
        reason = rest.strip() or "Marked complete by user."
        goal = complete_goal(thread_id, reason=reason)
        return _goal_action_response(goal, "Goal marked complete.", "No goal to complete.")
    goal = start_goal(thread_id, raw)
    return (
        f"Goal started: **{goal['objective']}**\n\n"
        f"Turn budget: {goal['turns_used']}/{goal['max_turns']}. "
        "Row-Bot will continue after each turn until the goal completes, pauses, blocks, or hits the budget."
    )


def is_goal_start_argument(arg: str) -> bool:
    raw = str(arg or "").strip()
    if not raw:
        return False
    return raw.split(maxsplit=1)[0].lower() not in GOAL_CONTROL_TOKENS


def format_goal_status(thread_id: str) -> str:
    goal = get_current_goal(thread_id, include_terminal=True)
    if not goal:
        return "No goal is active for this thread. Use `/goal <objective>` to start one."
    lines = [
        "**Goal**",
        f"Status: `{goal.get('status')}`",
        f"Objective: {goal.get('objective')}",
        f"Turns: {goal.get('turns_used', 0)}/{goal.get('max_turns', DEFAULT_GOAL_MAX_TURNS)}",
    ]
    if goal.get("last_progress"):
        lines.append(f"Progress: {goal.get('last_progress')}")
    if goal.get("last_reason"):
        lines.append(f"Reason: {goal.get('last_reason')}")
    evidence = goal.get("evidence_json") or []
    if evidence:
        lines.append("Evidence:")
        for item in evidence[-5:]:
            lines.append(f"- {item if isinstance(item, str) else json.dumps(item, sort_keys=True)}")
    blockers = goal.get("blockers_json") or []
    if blockers:
        lines.append("Blockers:")
        for item in blockers[-3:]:
            lines.append(f"- {item if isinstance(item, str) else json.dumps(item, sort_keys=True)}")
    return "\n".join(lines)


def build_initial_goal_prompt(goal: Mapping[str, Any]) -> str:
    objective = str(goal.get("objective") or "").strip()
    return (
        "[Goal mode started]\n"
        f"Objective: {objective}\n\n"
        "Work toward this objective now. Use the `goal_update` tool when you make meaningful progress, "
        "collect evidence, hit a blocker, choose a next step, or believe the goal is complete. "
        "Do not claim completion without evidence."
    )


def build_continuation_prompt(goal: Mapping[str, Any]) -> str:
    progress = str(goal.get("last_progress") or "").strip()
    reason = str(goal.get("last_reason") or "").strip()
    evidence = goal.get("evidence_json") or []
    blockers = goal.get("blockers_json") or []
    parts = [
        "[Goal continuation]",
        f"Objective: {goal.get('objective')}",
        f"Turns used: {goal.get('turns_used', 0)}/{goal.get('max_turns', DEFAULT_GOAL_MAX_TURNS)}",
    ]
    if progress:
        parts.append(f"Last progress: {progress}")
    if reason:
        parts.append(f"Last verifier/update reason: {reason}")
    if evidence:
        parts.append("Recent evidence: " + json.dumps(evidence[-5:], sort_keys=True))
    if blockers:
        parts.append("Recent blockers: " + json.dumps(blockers[-3:], sort_keys=True))
    parts.append(
        "Continue working toward the goal. If complete, call `goal_update` with status `completed` "
        "and cite evidence. If blocked, call `goal_update` with blockers and the user decision needed."
    )
    return "\n".join(parts)


def after_turn(
    *,
    thread_id: str,
    turn_id: str,
    assistant_text: str = "",
    model_override: str = "",
    pending_approval: bool = False,
    verifier: GoalVerifier | None = None,
) -> GoalContinuationDecision:
    """Evaluate an active goal after an assistant turn and maybe continue."""
    goal = _claim_turn(thread_id, turn_id)
    if not goal:
        return GoalContinuationDecision(None, False, reason="no active or already processed goal")
    if pending_approval:
        goal = set_goal_status(
            goal["id"],
            "waiting_approval",
            reason="Goal paused while approval is pending.",
            verdict="paused",
        ) or goal
        return GoalContinuationDecision(goal, False, reason="approval pending", status="waiting_approval")

    status = str(goal.get("status") or "")
    if status != "active":
        return GoalContinuationDecision(goal, False, reason=f"goal status is {status}", status=status)

    if int(goal.get("turns_used") or 0) >= int(goal.get("max_turns") or DEFAULT_GOAL_MAX_TURNS):
        goal = set_goal_status(
            goal["id"],
            "paused",
            reason="Turn budget reached. Resume extends the goal by another default budget window.",
            verdict="paused",
        ) or goal
        return GoalContinuationDecision(goal, False, reason="turn budget reached", status="paused")

    deterministic = _deterministic_goal_decision(goal)
    if deterministic in {"completed", "blocked", "paused"}:
        final_status = "completed" if deterministic == "completed" else "blocked" if deterministic == "blocked" else "paused"
        goal = set_goal_status(
            goal["id"],
            final_status,
            reason=str(goal.get("last_reason") or f"Goal {final_status}."),
            verdict="complete" if final_status == "completed" else final_status,
            finish_run_status="completed" if final_status == "completed" else "blocked" if final_status == "blocked" else "",
        ) or goal
        return GoalContinuationDecision(goal, False, reason=f"deterministic {final_status}", status=final_status)

    verifier_result = _verify_goal(
        goal,
        assistant_text=assistant_text,
        model_override=model_override,
        verifier=verifier,
    )
    latest_goal = get_goal(goal["id"])
    if not latest_goal or int(latest_goal.get("revision") or 0) != int(goal.get("revision") or 0):
        return GoalContinuationDecision(latest_goal, False, reason="goal changed during verification",
                                        status=str((latest_goal or {}).get("status") or "cleared"))
    verdict = _normalize_verdict(str(verifier_result.get("verdict") or "continue"))
    reason = str(verifier_result.get("reason") or "")
    if verdict == "complete":
        updated = set_goal_status(
            goal["id"],
            "completed",
            reason=reason or "Verifier agreed the goal is complete.",
            verdict="complete",
            finish_run_status="completed",
            expected_revision=int(goal.get("revision") or 0),
        )
        goal = updated or get_goal(goal["id"]) or goal
        return GoalContinuationDecision(goal, False, reason="verifier complete" if updated else "goal changed during verification", status=str(goal["status"]))
    if verdict in {"blocked", "needs_user"}:
        updated = set_goal_status(
            goal["id"],
            "blocked",
            reason=reason or "Verifier found the goal blocked.",
            verdict="blocked",
            finish_run_status="blocked",
            expected_revision=int(goal.get("revision") or 0),
        )
        goal = updated or get_goal(goal["id"]) or goal
        return GoalContinuationDecision(goal, False, reason="verifier blocked" if updated else "goal changed during verification", status=str(goal["status"]))
    if verdict == "paused":
        updated = set_goal_status(
            goal["id"],
            "paused",
            reason=reason or "Verifier paused the goal.",
            verdict="paused",
            expected_revision=int(goal.get("revision") or 0),
        )
        goal = updated or get_goal(goal["id"]) or goal
        return GoalContinuationDecision(goal, False, reason="verifier paused" if updated else "goal changed during verification", status=str(goal["status"]))
    if reason:
        updated = _record_verifier_reason(goal["id"], verdict, reason, expected_revision=int(goal.get("revision") or 0))
        if not updated:
            latest = get_goal(goal["id"]) or goal
            return GoalContinuationDecision(latest, False, reason="goal changed during verification", status=str(latest["status"]))
        goal = updated
    claimed = _claim_continuation(goal["id"], turn_id, expected_revision=int(goal.get("revision") or 0))
    if not claimed:
        latest = get_goal(goal["id"])
        return GoalContinuationDecision(latest or goal, False, reason="continuation already claimed or goal changed", status=str((latest or goal).get("status") or "cleared"))
    latest = get_goal(goal["id"]) or goal
    if latest.get("status") != "active" or int(latest.get("revision") or 0) != int(goal.get("revision") or 0) + 1:
        return GoalContinuationDecision(latest, False, reason="goal changed after continuation claim", status=str(latest.get("status") or "cleared"))
    active_run_id = str(latest.get("active_run_id") or "")
    if active_run_id:
        try:
            append_agent_event(
                active_run_id,
                "goal.continuation_requested",
                {
                    "goal_id": latest.get("id"),
                    "turn_id": turn_id,
                    "reason": reason or "goal incomplete",
                },
                visibility="log",
            )
        except Exception:
            pass
    return GoalContinuationDecision(
        latest,
        True,
        continuation_prompt=build_continuation_prompt(latest),
        reason=reason or "goal incomplete",
        status="active",
    )


def after_orchestration_completion(
    thread_id: str,
    orchestration_id: str,
    assistant_text: str,
) -> None:
    """Continue Goal Mode after a durable child barrier completes."""

    orchestration = None
    try:
        from row_bot.agent_orchestrator import get_orchestration

        orchestration = get_orchestration(orchestration_id)
    except Exception:
        logger.debug("Could not load completed Goal orchestration", exc_info=True)
    decision = after_turn(
        thread_id=thread_id,
        turn_id=f"orchestration:{orchestration_id}",
        assistant_text=assistant_text,
        model_override=str((orchestration or {}).get("model_ref") or ""),
    )
    if not decision.should_continue or not decision.continuation_prompt:
        return
    continuation = (orchestration or {}).get("continuation_state_json") or {}
    saved_config = continuation.get("config")
    config = dict(saved_config) if isinstance(saved_config, dict) else {
        "configurable": {}
    }
    config["configurable"] = dict(config.get("configurable") or {})
    next_generation_id = f"{thread_id}:goal:{uuid.uuid4().hex[:12]}"
    config["configurable"].update(
        {
            "thread_id": thread_id,
            "generation_id": next_generation_id,
            "runtime_surface": "goal",
            "runtime_mode": "agent",
            "root_objective": str(
                (decision.goal or {}).get("objective")
                or (orchestration or {}).get("root_objective")
                or decision.continuation_prompt
            ),
            "internal_goal_continuation": True,
        }
    )
    from row_bot.agent_orchestrator import start_parent_event_turn

    start_parent_event_turn(
        parent_thread_id=thread_id,
        parent_generation_id=next_generation_id,
        parent_run_id=str((decision.goal or {}).get("active_run_id") or ""),
        root_objective=str(
            (decision.goal or {}).get("objective")
            or (orchestration or {}).get("root_objective")
            or decision.continuation_prompt
        ),
        model_ref=str((orchestration or {}).get("model_ref") or ""),
        approval_mode=str((orchestration or {}).get("approval_mode") or ""),
        runtime_surface="goal",
        event_kind="goal_continuation",
        event_content=decision.continuation_prompt,
        source_event_id=(
            f"goal:{(decision.goal or {}).get('id') or thread_id}:"
            f"after:{orchestration_id}"
        ),
        config=config,
        enabled_tool_names=[
            str(name)
            for name in (continuation.get("enabled_tool_names") or [])
            if str(name)
        ],
        delivery_context={"runtime_surface": "goal"},
        event_payload={
            "goal_id": str((decision.goal or {}).get("id") or ""),
            "previous_orchestration_id": orchestration_id,
        },
    )


def _claim_turn(thread_id: str, turn_id: str) -> dict[str, Any] | None:
    if not thread_id or not turn_id:
        return None
    _ensure_goal_schema()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM thread_goals WHERE thread_id = ? "
            "AND status IN ('active', 'waiting_approval', 'paused', 'completed', 'blocked') "
            "ORDER BY updated_at DESC LIMIT 1",
            (str(thread_id),),
        ).fetchone()
        goal = _goal_from_row(row)
        if not goal:
            return None
        if conn.execute("INSERT OR IGNORE INTO goal_turn_claims(goal_id,turn_id) VALUES(?,?)",
                        (goal["id"], str(turn_id))).rowcount != 1:
            return None
        conn.execute(
            "UPDATE thread_goals SET turns_used = COALESCE(turns_used, 0) + 1, "
            "last_turn_id = ?, continuation_key = '', updated_at = ?, revision=revision+1 "
            "WHERE id = ? AND COALESCE(last_turn_id, '') != ?",
            (str(turn_id), now, str(goal["id"]), str(turn_id)),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    if not changed:
        return None
    return get_goal(goal["id"])


def _claim_continuation(goal_id: str, turn_id: str, *, expected_revision: int | None = None) -> bool:
    _ensure_goal_schema()
    key = f"{turn_id}:continue"
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE thread_goals SET continuation_key = ?, updated_at = ?, revision=revision+1 "
            "WHERE id = ? AND status = 'active' AND last_turn_id = ? "
            "AND COALESCE(continuation_key, '') != ? AND (? IS NULL OR revision=?)",
            (key, now, str(goal_id), str(turn_id), key, expected_revision, expected_revision),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    return bool(changed)


def _deterministic_goal_decision(goal: Mapping[str, Any]) -> str:
    status = str(goal.get("status") or "")
    if status == "completed" or str(goal.get("last_verdict") or "") == "complete":
        return "completed"
    if status == "blocked":
        return "blocked"
    if int(goal.get("blocker_count") or 0) >= 3:
        return "blocked"
    return "continue"


def _goal_child_agent_dependencies(goal: Mapping[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    thread_id = str(goal.get("thread_id") or "").strip()
    if not thread_id:
        return []
    try:
        from row_bot.agent_runs import list_agent_runs

        runs = list_agent_runs(parent_thread_id=thread_id, kind="subagent", limit=limit)
    except Exception:
        return []
    dependencies: list[dict[str, Any]] = []
    for run in runs[: max(1, int(limit or 12))]:
        dependencies.append(
            {
                "id": run.get("id"),
                "status": run.get("status"),
                "display_name": run.get("display_name"),
                "profile": run.get("profile_display_name") or run.get("profile_slug"),
                "summary": run.get("summary"),
                "status_message": run.get("status_message"),
                "error": run.get("error"),
                "turns_used": run.get("turns_used"),
                "max_turns": run.get("max_turns"),
            }
        )
    return dependencies


def _verify_goal(
    goal: Mapping[str, Any],
    *,
    assistant_text: str,
    model_override: str = "",
    verifier: GoalVerifier | None = None,
) -> Mapping[str, Any]:
    context = {
        "assistant_text": str(assistant_text or "")[-6000:],
        "model_override": str(model_override or ""),
        "child_agent_dependencies": _goal_child_agent_dependencies(goal),
    }
    try:
        result = verifier(dict(goal), context) if verifier else _invoke_goal_verifier(dict(goal), context)
        if not isinstance(result, Mapping):
            raise GoalError("Verifier returned a non-object result.")
        return result
    except Exception as exc:
        return _record_verifier_failure(
            str(goal.get("id") or ""),
            f"Verifier unavailable; continuing from goal_update/deterministic state: {exc}",
        )


def _invoke_goal_verifier(goal: dict[str, Any], context: dict[str, Any]) -> Mapping[str, Any]:
    """Run the default same-model verifier and return strict-ish JSON."""
    from row_bot.models import get_current_model, get_llm, get_llm_for, is_cloud_model, is_model_local

    model_ref = str(context.get("model_override") or get_current_model())
    if model_ref and model_ref != get_current_model() and (is_model_local(model_ref) or is_cloud_model(model_ref)):
        llm = get_llm_for(model_ref)
    else:
        llm = get_llm()
    system = (
        "You are Row-Bot's goal verifier. Return strict JSON only with keys "
        "`verdict` (continue|complete|blocked|needs_user|paused), `reason`, "
        "and optional `confidence` from 0 to 1. Do not use tools."
    )
    payload = {
        "objective": goal.get("objective"),
        "status": goal.get("status"),
        "turns_used": goal.get("turns_used"),
        "max_turns": goal.get("max_turns"),
        "last_progress": goal.get("last_progress"),
        "last_verdict": goal.get("last_verdict"),
        "last_reason": goal.get("last_reason"),
        "evidence": goal.get("evidence_json") or [],
        "blockers": goal.get("blockers_json") or [],
        "child_agent_dependencies": context.get("child_agent_dependencies") or [],
        "latest_assistant_response": context.get("assistant_text") or "",
    }
    response = llm.invoke(
        [
            {"role": "system", "content": system},
            {"role": "human", "content": json.dumps(payload, sort_keys=True)},
        ]
    )
    text = _content_to_text(getattr(response, "content", response))
    return _parse_verifier_json(text)


def _parse_verifier_json(text: str) -> Mapping[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise GoalError("Goal verifier JSON must be an object.")
    parsed["verdict"] = _normalize_verdict(str(parsed.get("verdict") or "continue"))
    parsed["reason"] = str(parsed.get("reason") or "")
    return parsed


def _record_verifier_failure(goal_id: str, reason: str) -> Mapping[str, Any]:
    if not goal_id:
        return {"verdict": "continue", "reason": reason}
    _ensure_goal_schema()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE thread_goals SET verifier_failures = COALESCE(verifier_failures, 0) + 1, "
            "last_verdict = 'continue', last_reason = ?, updated_at = ? WHERE id = ?",
            (reason, now, str(goal_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"verdict": "continue", "reason": reason}


def _record_verifier_reason(goal_id: str, verdict: str, reason: str, *, expected_revision: int | None = None) -> dict[str, Any] | None:
    _ensure_goal_schema()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE thread_goals SET last_verdict = ?, last_reason = ?, updated_at = ?,revision=revision+1 WHERE id = ? AND (? IS NULL OR revision=?)",
            (_normalize_verdict(verdict), str(reason or ""), now, str(goal_id), expected_revision, expected_revision),
        )
        changed = conn.total_changes
        conn.commit()
    finally:
        conn.close()
    return get_goal(goal_id) if changed else None


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item or ""))
        return "".join(parts)
    return str(content or "")


def _canonical_blocker(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, default=str)
    text = re.sub(r"\s+", " ", text.strip().lower())
    return text[:500]


def _reason_from_update(
    *,
    status: str,
    progress: str,
    blockers: list[Any],
    next_step: str,
) -> str:
    if status == "completed":
        return str(progress or "Goal marked complete.").strip()
    if status == "blocked":
        if blockers:
            return f"Blocked: {blockers[-1]}"
        return str(progress or "Goal marked blocked.").strip()
    if next_step:
        return f"Next: {next_step}"
    if blockers:
        return f"Blocker: {blockers[-1]}"
    return str(progress or "Goal progress updated.").strip()


def _short_objective(objective: str) -> str:
    text = re.sub(r"\s+", " ", str(objective or "")).strip()
    return text[:70] + ("..." if len(text) > 70 else "")


def _goal_action_response(goal: dict[str, Any] | None, success: str, missing: str) -> str:
    if not goal:
        return missing
    return f"{success}\n\n{format_goal_status(str(goal.get('thread_id') or ''))}"
