"""Buddy companion subsystem."""

from .brain import get_buddy_snapshot
from .events import BuddyEvent, BuddyEventType, emit_buddy_event, get_buddy_event_bus

__all__ = [
    "BuddyEvent",
    "BuddyEventType",
    "emit_buddy_event",
    "get_buddy_event_bus",
    "get_buddy_snapshot",
]
