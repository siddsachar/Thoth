"""Onboarding progress helpers for the first-run wizard and Setup Center."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ui.helpers import load_app_config, save_app_config


ONBOARDING_VERSION = 2

INTENT_OPTIONS: dict[str, str] = {
    "chat": "Chat assistant",
    "research": "Research and documents",
    "workflows": "Workflow automation",
    "designer": "Designer Studio",
    "channels": "Messaging channels",
    "local": "Local/private AI",
}

SETUP_STEPS: dict[str, dict[str, str]] = {
    "models": {
        "title": "Models",
        "description": "Connect a model provider and choose defaults.",
    },
    "knowledge": {
        "title": "Knowledge",
        "description": "Set up memory, documents, and embeddings.",
    },
    "workflows": {
        "title": "Workflows",
        "description": "Add starter workflows and delivery defaults.",
    },
    "designer": {
        "title": "Designer",
        "description": "Create design projects, decks, pages, and mockups.",
    },
    "channels": {
        "title": "Channels",
        "description": "Connect Telegram, WhatsApp, Discord, Slack, or SMS.",
    },
    "accounts": {
        "title": "Accounts",
        "description": "Connect Gmail, Calendar, X, and other accounts.",
    },
    "tools": {
        "title": "Tools & Skills",
        "description": "Review search, browser, shell, filesystem, and skills.",
    },
    "extensions": {
        "title": "MCP & Plugins",
        "description": "Add external tools through MCP servers and plugins.",
    },
    "voice": {
        "title": "Voice",
        "description": "Configure speech-to-text and text-to-speech.",
    },
    "final": {
        "title": "Final Check",
        "description": "Review readiness and fix anything missing.",
    },
}


def _clean_list(value: Any, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    clean: list[str] = []
    for item in value:
        key = str(item or "").strip()
        if not key or key in clean:
            continue
        if allowed is not None and key not in allowed:
            continue
        clean.append(key)
    return clean


def get_onboarding_state() -> dict[str, Any]:
    cfg = load_app_config()
    completed = _clean_list(
        cfg.get("onboarding_completed_steps"),
        set(SETUP_STEPS),
    )
    skipped = _clean_list(
        cfg.get("onboarding_skipped_steps"),
        set(SETUP_STEPS),
    )
    return {
        "version": int(cfg.get("onboarding_version") or 0),
        "profile": _clean_list(cfg.get("onboarding_profile"), set(INTENT_OPTIONS)),
        "completed_steps": completed,
        "skipped_steps": skipped,
        "dismissed_home_card": bool(cfg.get("onboarding_dismissed_home_card")),
        "last_seen": str(cfg.get("onboarding_last_seen") or ""),
        "setup_complete": bool(cfg.get("setup_complete")),
    }


def save_onboarding_profile(profile: list[str]) -> None:
    cfg = load_app_config()
    cfg["onboarding_version"] = ONBOARDING_VERSION
    cfg["onboarding_profile"] = _clean_list(profile, set(INTENT_OPTIONS))
    cfg["onboarding_last_seen"] = datetime.now().isoformat()
    save_app_config(cfg)


def request_setup_center_on_next_load() -> None:
    cfg = load_app_config()
    cfg["onboarding_open_setup_center_on_next_load"] = True
    cfg["onboarding_last_seen"] = datetime.now().isoformat()
    save_app_config(cfg)


def consume_setup_center_on_next_load() -> bool:
    cfg = load_app_config()
    should_open = bool(cfg.get("onboarding_open_setup_center_on_next_load"))
    if should_open:
        cfg["onboarding_open_setup_center_on_next_load"] = False
        cfg["onboarding_last_seen"] = datetime.now().isoformat()
        save_app_config(cfg)
    return should_open


def mark_onboarding_step(step: str, *, skipped: bool = False) -> None:
    if step not in SETUP_STEPS:
        raise ValueError(f"Unknown onboarding step: {step}")
    cfg = load_app_config()
    completed = _clean_list(cfg.get("onboarding_completed_steps"), set(SETUP_STEPS))
    skipped_steps = _clean_list(cfg.get("onboarding_skipped_steps"), set(SETUP_STEPS))
    if skipped:
        if step not in skipped_steps:
            skipped_steps.append(step)
        if step in completed:
            completed.remove(step)
    else:
        if step not in completed:
            completed.append(step)
        if step in skipped_steps:
            skipped_steps.remove(step)
    cfg["onboarding_version"] = ONBOARDING_VERSION
    cfg["onboarding_completed_steps"] = completed
    cfg["onboarding_skipped_steps"] = skipped_steps
    cfg["onboarding_last_seen"] = datetime.now().isoformat()
    save_app_config(cfg)


def dismiss_onboarding_home_card() -> None:
    cfg = load_app_config()
    cfg["onboarding_dismissed_home_card"] = True
    cfg["onboarding_last_seen"] = datetime.now().isoformat()
    save_app_config(cfg)


def reset_onboarding_home_card() -> None:
    cfg = load_app_config()
    cfg["onboarding_dismissed_home_card"] = False
    save_app_config(cfg)


def onboarding_progress() -> dict[str, Any]:
    state = get_onboarding_state()
    actionable = set(state["completed_steps"]) | set(state["skipped_steps"])
    total = len(SETUP_STEPS)
    done = len(actionable)
    return {
        **state,
        "done": done,
        "total": total,
        "percent": int((done / total) * 100) if total else 100,
        "complete": done >= total,
        "remaining_steps": [
            step for step in SETUP_STEPS
            if step not in actionable
        ],
    }
