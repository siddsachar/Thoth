"""Buddy public state model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class BuddyMood(StrEnum):
    CURIOUS = "curious"
    FOCUSED = "focused"
    EXCITED = "excited"
    CONCERNED = "concerned"
    PROUD = "proud"
    SLEEPY = "sleepy"


class BuddyMode(StrEnum):
    SIDEBAR = "sidebar"
    FLOATING = "floating"
    DESKTOP = "desktop"


@dataclass
class BuddyState:
    mood: BuddyMood = BuddyMood.CURIOUS
    animation: str = "idle_breathe"
    energy: int = 68
    focus: int = 20
    alert: int = 0
    message: str = "Idle"
    mode: BuddyMode = BuddyMode.SIDEBAR
    pack_id: str = "glyph"
    surface: str = "sidebar"
    event_id: int = 0
    updated_at: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mood"] = self.mood.value
        data["mode"] = self.mode.value
        return data
