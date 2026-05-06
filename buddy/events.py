"""Buddy event bus and event model."""

from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BuddyEventType(StrEnum):
    APP_READY = "app.ready"
    GENERATION_STARTED = "generation.started"
    THINKING = "generation.thinking"
    TOKEN = "generation.token"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    APPROVAL_NEEDED = "approval.needed"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_DENIED = "approval.denied"
    APPROVAL_TIMED_OUT = "approval.timed_out"
    GENERATION_DONE = "generation.done"
    GENERATION_ERROR = "generation.error"
    GENERATION_INTERRUPTED = "generation.interrupted"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_STEP = "workflow.step"
    WORKFLOW_DONE = "workflow.done"
    WORKFLOW_ERROR = "workflow.error"
    WORKFLOW_CANCELLED = "workflow.cancelled"
    NOTIFICATION = "notification"
    VOICE_LISTENING = "voice.listening"
    IDLE = "idle"


@dataclass(frozen=True)
class BuddyEvent:
    type: BuddyEventType
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    id: int = 0


class BuddyEventBus:
    """Small in-process pub/sub bus for background threads and UI clients."""

    def __init__(self, *, max_events: int = 512) -> None:
        self._max_events = max_events
        self._events: list[BuddyEvent] = []
        self._queue: queue.Queue[BuddyEvent] = queue.Queue()
        self._lock = threading.RLock()
        self._ids = itertools.count(1)

    def emit(
        self,
        event_type: BuddyEventType | str,
        *,
        source: str,
        payload: dict[str, Any] | None = None,
    ) -> BuddyEvent:
        try:
            typed_event = event_type if isinstance(event_type, BuddyEventType) else BuddyEventType(str(event_type))
        except ValueError:
            typed_event = BuddyEventType.NOTIFICATION
        event = BuddyEvent(
            type=typed_event,
            source=source,
            payload=dict(payload or {}),
            id=next(self._ids),
        )
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events:]
        self._queue.put(event)
        return event

    def recent(self, *, after_id: int = 0, limit: int = 100) -> list[BuddyEvent]:
        with self._lock:
            events = [event for event in self._events if event.id > after_id]
            return events[-limit:]

    def latest(self) -> BuddyEvent | None:
        with self._lock:
            return self._events[-1] if self._events else None


_bus = BuddyEventBus()


def get_buddy_event_bus() -> BuddyEventBus:
    return _bus


def emit_buddy_event(
    event_type: BuddyEventType | str,
    *,
    source: str,
    payload: dict[str, Any] | None = None,
) -> BuddyEvent:
    return _bus.emit(event_type, source=source, payload=payload)
