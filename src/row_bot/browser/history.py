"""Redacted Managed Browser history persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading

from row_bot.browser.policy import history_url
from row_bot.data_paths import get_row_bot_data_dir


HISTORY_PATH = get_row_bot_data_dir() / "browser_history.json"
_LOCK = threading.RLock()


def _load(path: Path) -> dict[str, list[dict]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def get_browser_history(thread_id: str, *, path: Path | None = None) -> list[dict]:
    with _LOCK:
        return _load(path or HISTORY_PATH).get(thread_id, [])


def append_browser_history(thread_id: str, entry: dict, *, path: Path | None = None) -> None:
    selected = path or HISTORY_PATH
    safe = dict(entry)
    safe.pop("text", None)
    if safe.get("url"):
        safe["url"] = history_url(str(safe["url"]))
    with _LOCK:
        value = _load(selected)
        value.setdefault(thread_id, []).append(safe)
        selected.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f"{selected.name}.", suffix=".tmp", dir=selected.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, default=str)
            Path(temporary).replace(selected)
        finally:
            Path(temporary).unlink(missing_ok=True)


def clear_browser_history(thread_id: str, *, path: Path | None = None) -> None:
    selected = path or HISTORY_PATH
    with _LOCK:
        value = _load(selected)
        if thread_id not in value:
            return
        del value[thread_id]
        selected.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f"{selected.name}.", suffix=".tmp", dir=selected.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, default=str)
            Path(temporary).replace(selected)
        finally:
            Path(temporary).unlink(missing_ok=True)
