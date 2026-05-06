"""Persistent Buddy configuration."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
from typing import Any

from .state import BuddyMode

logger = logging.getLogger(__name__)

_DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
_BUDDY_CONFIG_PATH = _DATA_DIR / "buddy_config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "sidebar_enabled": True,
    "floating_enabled": False,
    "desktop_enabled": False,
    "mode": BuddyMode.SIDEBAR.value,
    "pack_id": "glyph",
    "display_name": "Buddy",
    "personality": "warm_mystical",
    "personality_description": "Warm, curious, encouraging, and not too chatty.",
    "bubble_verbosity": "normal",
    "animation_intensity": "normal",
    "hatch_prompt": "A cute tiny mystical coding familiar for Thoth",
    "overlay": {"width": 260, "height": 260, "always_on_top": True},
}
_lock = threading.RLock()


def get_buddy_config() -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG)
    if _BUDDY_CONFIG_PATH.exists():
        try:
            stored = json.loads(_BUDDY_CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(stored, dict):
                cfg.update(stored)
        except Exception:
            logger.warning("Failed to load Buddy config from %s", _BUDDY_CONFIG_PATH, exc_info=True)
    return cfg


def save_buddy_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG)
    cfg.update(config)
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


def reset_buddy_config() -> dict[str, Any]:
    return save_buddy_config(dict(_DEFAULT_CONFIG))
