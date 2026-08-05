"""Deterministic Buddy behavior resolver.

Buddy is driven by many producers: chat streaming, tool calls, workflows,
approvals, voice, and background work.  This resolver keeps those signals in a
small owned state machine so a stale "approval needed" or "workflow running"
state can only remain active while the owning fact is still active.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from .config import get_buddy_config
from .events import BuddyEvent, BuddyEventType, get_buddy_event_bus
from .state import BuddyMode, BuddyMood, BuddyState

logger = logging.getLogger(__name__)


_EVENT_REACTIONS: dict[BuddyEventType, tuple[BuddyMood, str, int, int, int, str]] = {
    BuddyEventType.APP_READY: (BuddyMood.CURIOUS, "wake", 72, 16, 0, "Ready"),
    BuddyEventType.GENERATION_STARTED: (BuddyMood.FOCUSED, "lean_in", 76, 72, 0, "Thinking"),
    BuddyEventType.THINKING: (BuddyMood.FOCUSED, "think_loop", 70, 86, 0, "Reasoning"),
    BuddyEventType.TOKEN: (BuddyMood.FOCUSED, "type_follow", 66, 78, 0, "Writing"),
    BuddyEventType.TOOL_STARTED: (BuddyMood.EXCITED, "tool_peek", 82, 68, 10, "Using a tool"),
    BuddyEventType.TOOL_FINISHED: (BuddyMood.PROUD, "nod", 78, 54, 0, "Tool finished"),
    BuddyEventType.APPROVAL_NEEDED: (BuddyMood.CONCERNED, "tap_glass", 84, 90, 86, "Needs approval"),
    BuddyEventType.APPROVAL_APPROVED: (BuddyMood.PROUD, "nod", 78, 42, 0, "Approved"),
    BuddyEventType.APPROVAL_DENIED: (BuddyMood.CONCERNED, "pause", 48, 46, 24, "Denied"),
    BuddyEventType.APPROVAL_TIMED_OUT: (BuddyMood.CONCERNED, "pause", 44, 42, 34, "Approval timed out"),
    BuddyEventType.GENERATION_INTERRUPTED: (BuddyMood.CONCERNED, "pause", 58, 62, 65, "Paused"),
    BuddyEventType.GENERATION_STOPPED: (BuddyMood.CONCERNED, "pause", 48, 30, 18, "Stopped"),
    BuddyEventType.GENERATION_DONE: (BuddyMood.PROUD, "celebrate_small", 88, 28, 0, "Done"),
    BuddyEventType.GENERATION_ERROR: (BuddyMood.CONCERNED, "worry", 42, 44, 95, "Error"),
    BuddyEventType.ORCHESTRATION_ACTIVE: (BuddyMood.FOCUSED, "think_loop", 72, 82, 12, "Agents working"),
    BuddyEventType.ORCHESTRATION_DONE: (BuddyMood.PROUD, "nod", 78, 32, 0, "Agent work done"),
    BuddyEventType.WORKFLOW_STARTED: (BuddyMood.EXCITED, "pack_bag", 82, 65, 0, "Workflow running"),
    BuddyEventType.WORKFLOW_STEP: (BuddyMood.FOCUSED, "step_check", 74, 76, 0, "Workflow step"),
    BuddyEventType.WORKFLOW_DONE: (BuddyMood.PROUD, "celebrate_big", 90, 25, 0, "Workflow done"),
    BuddyEventType.WORKFLOW_ERROR: (BuddyMood.CONCERNED, "worry", 40, 52, 92, "Workflow error"),
    BuddyEventType.WORKFLOW_CANCELLED: (BuddyMood.CONCERNED, "pause", 48, 38, 24, "Workflow cancelled"),
    BuddyEventType.NOTIFICATION: (BuddyMood.EXCITED, "ping", 80, 35, 36, "Notification"),
    BuddyEventType.VOICE_LISTENING: (BuddyMood.CURIOUS, "listen", 78, 62, 8, "Listening"),
    BuddyEventType.IDLE: (BuddyMood.CURIOUS, "idle_breathe", 64, 20, 0, "Idle"),
}

_IDLE_GRACE_SECONDS = 2.0
_STALE_ACTIVITY_SECONDS = 120.0
_DURABLE_RECONCILE_SECONDS = 5.0
_ACTIVE_PRIORITY = ("approval", "tool", "generation", "orchestration", "workflow", "voice")
_ACTIVITY_STARTS: dict[BuddyEventType, str] = {
    BuddyEventType.GENERATION_STARTED: "generation",
    BuddyEventType.THINKING: "generation",
    BuddyEventType.TOKEN: "generation",
    BuddyEventType.ORCHESTRATION_ACTIVE: "orchestration",
    BuddyEventType.TOOL_STARTED: "tool",
    BuddyEventType.APPROVAL_NEEDED: "approval",
    BuddyEventType.WORKFLOW_STARTED: "workflow",
    BuddyEventType.WORKFLOW_STEP: "workflow",
    BuddyEventType.VOICE_LISTENING: "voice",
}
_ACTIVITY_ENDS: dict[BuddyEventType, tuple[str, ...]] = {
    BuddyEventType.TOOL_FINISHED: ("tool",),
    BuddyEventType.APPROVAL_APPROVED: ("approval",),
    BuddyEventType.APPROVAL_DENIED: ("approval", "workflow"),
    BuddyEventType.APPROVAL_TIMED_OUT: ("approval", "workflow"),
    BuddyEventType.GENERATION_DONE: ("generation", "tool", "approval"),
    BuddyEventType.GENERATION_ERROR: ("generation", "tool", "approval"),
    BuddyEventType.GENERATION_INTERRUPTED: ("generation", "tool"),
    BuddyEventType.GENERATION_STOPPED: ("generation", "tool", "approval"),
    BuddyEventType.ORCHESTRATION_DONE: ("orchestration",),
    BuddyEventType.WORKFLOW_DONE: ("workflow",),
    BuddyEventType.WORKFLOW_ERROR: ("workflow",),
    BuddyEventType.WORKFLOW_CANCELLED: ("workflow",),
    BuddyEventType.IDLE: ("generation", "tool", "approval", "workflow", "voice"),
}


@dataclass
class _Activity:
    kind: str
    event_type: BuddyEventType
    owner_id: str
    source: str
    label: str
    payload: dict[str, Any]
    updated_at: float
    reconciled: bool = False


def _state_event_type(state: BuddyState) -> BuddyEventType | None:
    try:
        return BuddyEventType(str(state.details.get("event_type") or ""))
    except ValueError:
        return None


def _owner_from_payload(kind: str, payload: dict[str, Any]) -> str:
    if kind == "approval":
        return str(
            payload.get("approval_id")
            or payload.get("resume_token")
            or payload.get("request_id")
            or "approval"
        )
    if kind == "workflow":
        return str(
            payload.get("run_id")
            or payload.get("thread_id")
            or payload.get("task_id")
            or "workflow"
        )
    if kind == "generation":
        return str(payload.get("thread_id") or "generation")
    if kind == "tool":
        return str(payload.get("tool_call_id") or payload.get("tool") or "tool")
    if kind == "orchestration":
        return str(payload.get("orchestration_id") or payload.get("thread_id") or "orchestration")
    if kind == "voice":
        return "voice"
    return kind


def _approval_key_values(payload: dict[str, Any], owner_id: str = "") -> set[str]:
    keys = {
        owner_id,
        str(payload.get("approval_id") or ""),
        str(payload.get("resume_token") or ""),
        str(payload.get("request_id") or ""),
        str(payload.get("run_id") or ""),
        str(payload.get("step_id") or ""),
    }
    task_id = str(payload.get("task_id") or "")
    if task_id and (payload.get("approval_id") or payload.get("resume_token") or payload.get("step_id")):
        keys.add(task_id)
    return {key for key in keys if key}


def _is_durable_approval_activity(active: _Activity) -> bool:
    payload = active.payload
    if active.source in {"tasks", "tasks.reconcile"}:
        return True
    return bool(
        payload.get("resume_token")
        or payload.get("request_id")
        or payload.get("run_id")
        or payload.get("step_id")
    )


def _approval_activity_is_pending(active: _Activity, pending: list[dict[str, Any]]) -> bool:
    active_keys = _approval_key_values(active.payload, active.owner_id)
    if not active_keys:
        return False
    for approval in pending:
        pending_keys = _approval_key_values(
            {
                "approval_id": approval.get("id"),
                "resume_token": approval.get("resume_token"),
                "run_id": approval.get("run_id"),
                "task_id": approval.get("task_id"),
                "step_id": approval.get("step_id"),
            },
            str(approval.get("id") or approval.get("resume_token") or ""),
        )
        if active_keys & pending_keys:
            return True
    return False


class BuddyBrain:
    """Maps Row-Bot events into compact Buddy runtime values."""

    def __init__(self) -> None:
        cfg = get_buddy_config()
        self._state = BuddyState(
            mode=BuddyMode(str(cfg.get("mode", BuddyMode.SIDEBAR.value))),
            pack_id=str(cfg.get("pack_id", "glyph")),
            updated_at=time.time(),
        )
        self._last_event_id = 0
        self._active: dict[str, dict[str, _Activity]] = {}
        self._last_reconcile_at = 0.0
        self._sequence = 0

    @property
    def state(self) -> BuddyState:
        return self._state

    def resolve(self, event: BuddyEvent | None) -> BuddyState:
        cfg = get_buddy_config()
        now = time.time()
        if not cfg.get("enabled", True):
            self._state = replace(
                self._state,
                mood=BuddyMood.SLEEPY,
                animation="sleep",
                energy=20,
                focus=0,
                alert=0,
                message="Disabled",
                updated_at=now,
            )
            self._active.clear()
            return self._state

        if event is None:
            self._reconcile_durable_activity(now)
            self._prune_stale_activity(now)
            activity = self._dominant_activity()
            state_event_type = _state_event_type(self._state)
            if activity and state_event_type == activity.event_type:
                return self._state
            if activity is None:
                if self._state.animation == "idle_breathe" or now - self._state.updated_at <= _IDLE_GRACE_SECONDS:
                    return self._state
                event_type = BuddyEventType.IDLE
                event_id = self._state.event_id
                source = "brain"
                payload: dict[str, Any] = {}
            elif now - self._state.updated_at <= _IDLE_GRACE_SECONDS:
                return self._state
            else:
                event_type = activity.event_type
                event_id = self._state.event_id
                source = activity.source
                payload = dict(activity.payload)
                payload.setdefault("label", activity.label)
                payload.setdefault("owner_id", activity.owner_id)
        else:
            if event.id <= self._last_event_id:
                return self._state
            event_type = event.type
            event_id = event.id
            source = event.source
            payload = dict(event.payload)
            self._last_event_id = event.id
            self._apply_event_to_activities(event_type, source, payload, now)
            if event_type == BuddyEventType.ORCHESTRATION_DONE:
                remaining = self._dominant_activity()
                if remaining is not None:
                    event_type = remaining.event_type
                    source = remaining.source
                    payload = dict(remaining.payload)
                    payload.setdefault("label", remaining.label)
                    payload.setdefault("owner_id", remaining.owner_id)

        return self._build_state(event_type, event_id, source, payload, cfg, now)

    def _build_state(
        self,
        event_type: BuddyEventType,
        event_id: int,
        source: str,
        payload: dict[str, Any],
        cfg: dict,
        now: float,
    ) -> BuddyState:
        previous = self._state
        mood, animation, energy, focus, alert, message = _EVENT_REACTIONS.get(
            event_type,
            _EVENT_REACTIONS[BuddyEventType.IDLE],
        )
        self._sequence += 1
        details = {
            "source": source,
            "event_type": event_type.value,
            "sequence": self._sequence,
            **payload,
        }
        self._state = BuddyState(
            mood=mood,
            animation=animation,
            energy=max(0, min(100, energy)),
            focus=max(0, min(100, focus)),
            alert=max(0, min(100, alert)),
            message=str(payload.get("label") or message),
            mode=BuddyMode(str(cfg.get("mode", BuddyMode.SIDEBAR.value))),
            pack_id=str(cfg.get("pack_id", "glyph")),
            event_id=event_id,
            updated_at=now,
            details=details,
        )
        if previous.animation != self._state.animation or previous.message != self._state.message:
            logger.info(
                "buddy_state: %s/%s -> %s/%s source=%s owner=%s",
                previous.details.get("event_type", "unknown"),
                previous.message,
                event_type.value,
                self._state.message,
                source,
                payload.get("owner_id") or payload.get("approval_id") or payload.get("thread_id") or payload.get("task_id") or "",
            )
        return self._state

    def _apply_event_to_activities(
        self,
        event_type: BuddyEventType,
        source: str,
        payload: dict[str, Any],
        now: float,
    ) -> None:
        for activity in _ACTIVITY_ENDS.get(event_type, ()):
            self._clear_activity(activity, payload)
        activity = _ACTIVITY_STARTS.get(event_type)
        if activity:
            owner_id = _owner_from_payload(activity, payload)
            self._active.setdefault(activity, {})[owner_id] = _Activity(
                kind=activity,
                event_type=event_type,
                owner_id=owner_id,
                source=source,
                label=str(payload.get("label") or _EVENT_REACTIONS[event_type][5]),
                payload=dict(payload),
                updated_at=now,
            )

    def _clear_activity(self, activity: str, payload: dict[str, Any]) -> None:
        owners = self._active.get(activity)
        if not owners:
            return
        owner_id = _owner_from_payload(activity, payload)
        if owner_id in owners:
            owners.pop(owner_id, None)
        if activity == "approval":
            owners.pop("approval", None)
            run_id = str(payload.get("run_id") or "")
            task_id = str(payload.get("task_id") or "")
            step_id = str(payload.get("step_id") or "")
            resume_token = str(payload.get("resume_token") or "")
            for existing_owner, active in list(owners.items()):
                active_payload = active.payload
                if (
                    (run_id and str(active_payload.get("run_id") or "") == run_id)
                    or (task_id and str(active_payload.get("task_id") or "") == task_id)
                    or (step_id and str(active_payload.get("step_id") or "") == step_id)
                    or (resume_token and str(active_payload.get("resume_token") or "") == resume_token)
                ):
                    owners.pop(existing_owner, None)
        elif owner_id == activity or owner_id in {"approval", "workflow", "generation", "tool", "voice"}:
            owners.clear()
        elif activity == "workflow" and (
            payload.get("approval_id") or payload.get("run_id") or payload.get("status")
        ):
            owners.clear()
        if not owners:
            self._active.pop(activity, None)

    def _prune_stale_activity(self, now: float) -> None:
        for activity, owners in list(self._active.items()):
            for owner_id, active in list(owners.items()):
                if active.reconciled:
                    continue
                if now - active.updated_at > _STALE_ACTIVITY_SECONDS:
                    logger.info(
                        "buddy_state: pruning stale activity kind=%s owner=%s event=%s",
                        activity,
                        owner_id,
                        active.event_type.value,
                    )
                    owners.pop(owner_id, None)
            if not owners:
                self._active.pop(activity, None)

    def _dominant_activity(self) -> _Activity | None:
        for activity in _ACTIVE_PRIORITY:
            owners = self._active.get(activity)
            if owners:
                return max(owners.values(), key=lambda item: item.updated_at)
        return None

    def _reconcile_durable_activity(self, now: float) -> None:
        if now - self._last_reconcile_at < _DURABLE_RECONCILE_SECONDS:
            return
        self._last_reconcile_at = now
        try:
            from row_bot.tasks import get_pending_approvals, get_running_tasks
            from row_bot.agent_orchestrator import get_thread_orchestration_activity
            pending = get_pending_approvals()
            running = get_running_tasks()
            orchestration_activity = get_thread_orchestration_activity()
        except Exception:
            return

        approval_owners = self._active.setdefault("approval", {})
        for owner_id, active in list(approval_owners.items()):
            if active.reconciled:
                approval_owners.pop(owner_id, None)
            elif _is_durable_approval_activity(active) and not _approval_activity_is_pending(active, pending):
                approval_owners.pop(owner_id, None)
        for approval in pending:
            owner_id = str(approval.get("id") or approval.get("resume_token") or "approval")
            approval_owners[owner_id] = _Activity(
                kind="approval",
                event_type=BuddyEventType.APPROVAL_NEEDED,
                owner_id=owner_id,
                source="tasks.reconcile",
                label=str(approval.get("task_name") or "Needs approval"),
                payload={
                    "approval_id": owner_id,
                    "resume_token": str(approval.get("resume_token") or ""),
                    "run_id": str(approval.get("run_id") or ""),
                    "task_id": str(approval.get("task_id") or ""),
                    "step_id": str(approval.get("step_id") or ""),
                    "label": str(approval.get("task_name") or "Needs approval"),
                },
                updated_at=now,
                reconciled=True,
            )
        if not approval_owners:
            self._active.pop("approval", None)

        workflow_owners = self._active.setdefault("workflow", {})
        for owner_id, active in list(workflow_owners.items()):
            if active.reconciled:
                workflow_owners.pop(owner_id, None)
        for thread_id, info in running.items():
            owner_id = str(info.get("run_id") or thread_id)
            workflow_owners[owner_id] = _Activity(
                kind="workflow",
                event_type=BuddyEventType.WORKFLOW_STARTED,
                owner_id=owner_id,
                source="tasks.reconcile",
                label=str(info.get("name") or "Workflow running"),
                payload={
                    "thread_id": thread_id,
                    "task_id": info.get("task_id", ""),
                    "run_id": info.get("run_id", ""),
                    "label": str(info.get("name") or "Workflow running"),
                },
                updated_at=now,
                reconciled=True,
            )
        if not workflow_owners:
            self._active.pop("workflow", None)

        orchestration_owners = self._active.setdefault("orchestration", {})
        durable_orchestration_ids = {
            str(info.get("orchestration_id") or thread_id)
            for thread_id, info in orchestration_activity.items()
            if str(info.get("state") or "") == "active"
        }
        for owner_id in list(orchestration_owners):
            if owner_id not in durable_orchestration_ids:
                orchestration_owners.pop(owner_id, None)
        phase_labels = {
            "child_running": "Child Agents working",
            "later_wave_parent": "Preparing the next Agent wave",
            "approval_wait": "Agent group needs approval",
            "retry": "Retrying Agent work",
            "stopping": "Stopping Agent work",
            "background": "Background Agent working",
        }
        for thread_id, info in orchestration_activity.items():
            if str(info.get("state") or "") != "active":
                continue
            owner_id = str(info.get("orchestration_id") or thread_id)
            phase = str(info.get("phase") or "child_running")
            label = phase_labels.get(phase, "Agents working")
            orchestration_owners[owner_id] = _Activity(
                kind="orchestration",
                event_type=BuddyEventType.ORCHESTRATION_ACTIVE,
                owner_id=owner_id,
                source="agent_orchestrator.reconcile",
                label=label,
                payload={
                    "orchestration_id": owner_id,
                    "thread_id": thread_id,
                    "phase": phase,
                    "blocking": bool(info.get("blocking")),
                    "background": bool(info.get("background")),
                    "active_members": int(info.get("active_members") or 0),
                    "failed_members": int(info.get("failed_members") or 0),
                    "label": label,
                },
                updated_at=now,
                reconciled=True,
            )
        if not orchestration_owners:
            self._active.pop("orchestration", None)

    def tick(self) -> BuddyState:
        events = get_buddy_event_bus().recent(after_id=self._last_event_id, limit=512)
        if not events:
            return self.resolve(None)
        state = self._state
        for event in events:
            state = self.resolve(event)
        return state


_brain = BuddyBrain()


def get_buddy_brain() -> BuddyBrain:
    return _brain


def get_buddy_snapshot() -> dict:
    return _brain.tick().to_dict()
