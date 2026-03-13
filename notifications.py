"""Unified notification system — desktop alerts, sounds, and in-app toasts.

All background subsystems (workflows, timers) call ``notify()`` to fire
an immediate desktop notification + sound, and queue a toast message for
the next Streamlit rerun.
"""

from __future__ import annotations

import logging
import pathlib
import queue
import subprocess
import sys

logger = logging.getLogger(__name__)

# ── Toast queue (thread-safe) ────────────────────────────────────────────────
# Background threads push messages here; the Streamlit render loop drains them.
_toast_queue: queue.Queue[dict] = queue.Queue()

# ── Sound files ──────────────────────────────────────────────────────────────
_SOUNDS_DIR = pathlib.Path(__file__).parent / "sounds"
_SOUND_MAP: dict[str, pathlib.Path] = {
    "workflow": _SOUNDS_DIR / "workflow.wav",
    "timer": _SOUNDS_DIR / "timer.wav",
}


def notify(
    title: str,
    message: str,
    sound: str = "default",
    icon: str = "🔔",
) -> None:
    """Fire a notification through all channels.

    Parameters
    ----------
    title : str
        Notification title (shown in desktop toast and plyer).
    message : str
        Notification body text.
    sound : str
        Sound key: ``"workflow"``, ``"timer"``, or ``"default"``
        (falls back to Windows system beep).
    icon : str
        Emoji prefix for the Streamlit ``st.toast()`` message.
    """
    from datetime import datetime
    timestamp = datetime.now().strftime("%I:%M %p")

    # 1. Desktop notification (plyer) — immediate
    _desktop_notify(title, f"{message} ({timestamp})")

    # 2. Sound — immediate, non-blocking
    _play_sound(sound)

    # 3. Queue toast for next Streamlit rerun
    _toast_queue.put({"icon": icon, "message": f"{message} ({timestamp})"})


def drain_toasts() -> list[dict]:
    """Drain all pending toast messages (called by the Streamlit render loop).

    Returns a list of ``{"icon": str, "message": str}`` dicts.
    """
    toasts: list[dict] = []
    while True:
        try:
            toasts.append(_toast_queue.get_nowait())
        except queue.Empty:
            break
    return toasts


# ── Internal helpers ─────────────────────────────────────────────────────────

def _desktop_notify(title: str, message: str) -> None:
    """Show a desktop notification via plyer."""
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name="Thoth",
            timeout=15,
        )
    except Exception:
        logger.debug("Desktop notification failed (non-fatal)")


def _play_sound(sound: str) -> None:
    """Play a notification sound asynchronously."""
    try:
        wav_path = _SOUND_MAP.get(sound)

        if sys.platform == "win32":
            import winsound
            if wav_path and wav_path.exists():
                winsound.PlaySound(
                    str(wav_path),
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
            else:
                # Fallback: Windows system asterisk sound
                winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)

        elif sys.platform == "darwin":
            if wav_path and wav_path.exists():
                subprocess.Popen(["afplay", str(wav_path)])
            else:
                # Fallback: macOS built-in Glass sound
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])

        else:
            # Linux — use aplay (ALSA utils) if available
            if wav_path and wav_path.exists():
                subprocess.Popen(["aplay", "-q", str(wav_path)])

    except Exception:
        logger.debug("Sound playback failed (non-fatal)")
