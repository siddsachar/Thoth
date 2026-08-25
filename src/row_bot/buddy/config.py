"""Persistent Buddy configuration."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
from typing import Any

from row_bot.data_paths import get_row_bot_data_dir
from .overlay import (
    OVERLAY_HEIGHT,
    OVERLAY_WIDTH,
    BuddyPlacement,
    BuddyPlacementState,
    apply_placement_state,
    placement_state_from_config,
)
from .state import BuddyMode

logger = logging.getLogger(__name__)

_DATA_DIR = get_row_bot_data_dir()
_BUDDY_CONFIG_PATH = _DATA_DIR / "buddy_config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "placement": BuddyPlacement.DOCKED.value,
    "visible": True,
    "collapsed": False,
    "tear_off_hint_dismissed": False,
    "mode": BuddyMode.SIDEBAR.value,
    "pack_id": "glyph",
    "display_name": "Buddy",
    "personality": "warm_mystical",
    "personality_description": "Warm, curious, encouraging, and not too chatty.",
    "bubble_verbosity": "normal",
    "animation_intensity": "normal",
    "hatch_prompt": "A cute tiny mystical coding familiar for Row-Bot",
    "overlay": {
        "width": OVERLAY_WIDTH,
        "height": OVERLAY_HEIGHT,
        "always_on_top": True,
        "x": None,
        "y": None,
    },
}
_LEGACY_SURFACE_KEYS = {"sidebar_enabled", "floating_enabled", "desktop_enabled"}
_lock = threading.RLock()


def _normalize_config(stored: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(stored or {})
    placement_state = placement_state_from_config(raw)
    cfg = dict(_DEFAULT_CONFIG)
    cfg.update(raw)
    overlay = dict(_DEFAULT_CONFIG["overlay"])
    if isinstance(raw.get("overlay"), dict):
        overlay.update(raw["overlay"])
    # The revamped overlay is fixed-size. Legacy 260x260 values must not keep
    # driving native placement calculations after migration.
    overlay.update(
        {
            "width": OVERLAY_WIDTH,
            "height": OVERLAY_HEIGHT,
            "always_on_top": True,
        }
    )
    cfg["overlay"] = overlay
    cfg = apply_placement_state(cfg, placement_state)
    # ``visible`` is the canonical state. Keep the historical flag derived so
    # BuddyBrain and one-release-old integrations cannot drift from it.
    cfg["enabled"] = placement_state.visible
    for key in _LEGACY_SURFACE_KEYS:
        cfg.pop(key, None)
    return cfg


def get_buddy_config() -> dict[str, Any]:
    stored: dict[str, Any] = {}
    if _BUDDY_CONFIG_PATH.exists():
        try:
            loaded = json.loads(_BUDDY_CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                stored = loaded
        except Exception:
            logger.warning("Failed to load Buddy config from %s", _BUDDY_CONFIG_PATH, exc_info=True)
    return _normalize_config(stored)


def save_buddy_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = _normalize_config(config)
    with _lock:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _BUDDY_CONFIG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            logger.warning("Failed to save Buddy config to %s", _BUDDY_CONFIG_PATH, exc_info=True)
    return cfg


def set_buddy_config(key: str, value: Any) -> dict[str, Any]:
    cfg = get_buddy_config()
    cfg[key] = value
    return save_buddy_config(cfg)


def set_buddy_placement_state(state: BuddyPlacementState) -> dict[str, Any]:
    """Persist one canonical placement transition."""

    return save_buddy_config(apply_placement_state(get_buddy_config(), state))


def get_buddy_placement_state() -> BuddyPlacementState:
    return placement_state_from_config(get_buddy_config())


def reset_buddy_config() -> dict[str, Any]:
    return save_buddy_config(dict(_DEFAULT_CONFIG))
