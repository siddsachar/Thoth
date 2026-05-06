"""Deterministic Buddy behavior resolver."""

from __future__ import annotations

import time
from dataclasses import replace

from .config import get_buddy_config
from .events import BuddyEvent, BuddyEventType, get_buddy_event_bus
from .state import BuddyMode, BuddyMood, BuddyState


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
    BuddyEventType.GENERATION_DONE: (BuddyMood.PROUD, "celebrate_small", 88, 28, 0, "Done"),
    BuddyEventType.GENERATION_ERROR: (BuddyMood.CONCERNED, "worry", 42, 44, 95, "Error"),
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
_ACTIVE_PRIORITY = ("approval", "tool", "generation", "workflow", "voice")
_ACTIVITY_STARTS: dict[BuddyEventType, str] = {
    BuddyEventType.GENERATION_STARTED: "generation",
    BuddyEventType.THINKING: "generation",
    BuddyEventType.TOKEN: "generation",
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
    BuddyEventType.WORKFLOW_DONE: ("workflow",),
    BuddyEventType.WORKFLOW_ERROR: ("workflow",),
    BuddyEventType.WORKFLOW_CANCELLED: ("workflow",),
    BuddyEventType.IDLE: ("generation", "tool", "approval", "workflow", "voice"),
}


def _state_event_type(state: BuddyState) -> BuddyEventType | None:
    try:
        return BuddyEventType(str(state.details.get("event_type") or ""))
    except ValueError:
        return None


class BuddyBrain:
    """Maps Thoth events into compact Buddy runtime values."""

    def __init__(self) -> None:
        cfg = get_buddy_config()
        self._state = BuddyState(
            mode=BuddyMode(str(cfg.get("mode", BuddyMode.SIDEBAR.value))),
            pack_id=str(cfg.get("pack_id", "glyph")),
            updated_at=time.time(),
        )
        self._last_event_id = 0
        self._active: dict[str, tuple[BuddyEventType, float]] = {}

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
            self._prune_stale_activity(now)
            event_type = self._dominant_active_event_type()
            state_event_type = _state_event_type(self._state)
            if event_type and state_event_type == event_type:
                return self._state
            if event_type is None:
                if self._state.animation == "idle_breathe" or now - self._state.updated_at <= _IDLE_GRACE_SECONDS:
                    return self._state
                event_type = BuddyEventType.IDLE
            elif now - self._state.updated_at <= _IDLE_GRACE_SECONDS:
                return self._state
            event_id = self._state.event_id
            source = "brain"
            payload = {}
        else:
            if event.id <= self._last_event_id:
                return self._state
            event_type = event.type
            event_id = event.id
            source = event.source
            payload = event.payload
            self._last_event_id = event.id
            self._update_activity(event_type, now)

        mood, animation, energy, focus, alert, message = _EVENT_REACTIONS.get(
            event_type,
            _EVENT_REACTIONS[BuddyEventType.IDLE],
        )
        details = {"source": source, "event_type": event_type.value, **payload}
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
        return self._state

    def _update_activity(self, event_type: BuddyEventType, now: float) -> None:
        for activity in _ACTIVITY_ENDS.get(event_type, ()):
            self._active.pop(activity, None)
        activity = _ACTIVITY_STARTS.get(event_type)
        if activity:
            self._active[activity] = (event_type, now)
    def _prune_stale_activity(self, now: float) -> None:
        for activity, (_, updated_at) in list(self._active.items()):
            if now - updated_at > _STALE_ACTIVITY_SECONDS:
                self._active.pop(activity, None)

    def _dominant_active_event_type(self) -> BuddyEventType | None:
        for activity in _ACTIVE_PRIORITY:
            if activity in self._active:
                return self._active[activity][0]
        return None

    def tick(self) -> BuddyState:
        latest = get_buddy_event_bus().latest()
        if latest is not None and latest.id <= self._last_event_id:
            return self.resolve(None)
        return self.resolve(latest)


_brain = BuddyBrain()


def get_buddy_brain() -> BuddyBrain:
    return _brain


def get_buddy_snapshot() -> dict:
    return _brain.tick().to_dict()
