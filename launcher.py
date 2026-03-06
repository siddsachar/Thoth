"""Thoth Launcher — system-tray process that manages the Streamlit server.

Responsibilities:
    • System-tray icon with status colors (green / yellow / grey)
    • Launch  ``streamlit run app.py``  as a managed subprocess
    • Open the browser to http://localhost:8501
    • Poll ~/.thoth/status.json to update the tray icon / tooltip
    • Detect an already-running instance and just open the browser
    • Graceful shutdown on Quit
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image as _PILImage

# ── Setup logging ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
_PORT = 8501
_URL = f"http://localhost:{_PORT}"
_STATUS_FILE = Path.home() / ".thoth" / "status.json"
_POLL_INTERVAL = 1.5          # seconds between status-file reads
_STARTUP_GRACE = 6            # seconds to wait for Streamlit before opening browser
_ICON_SIZE = 64               # px for generated tray icons
_STALE_THRESHOLD = 10         # seconds before status is considered stale


# ── Icon generation (Pillow, no external files) ──────────────────────────────

def _make_icon(colour: str) -> _PILImage.Image:
    """Create a solid circle icon with the given colour on a transparent bg."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse(
        [margin, margin, _ICON_SIZE - margin, _ICON_SIZE - margin],
        fill=colour,
    )
    return img


# Pre-generate the three state icons lazily
_icons: dict[str, _PILImage.Image] = {}


def _get_icon(state: str) -> _PILImage.Image:
    """Return the icon for a voice state string."""
    colour_map = {
        "listening":    "#22c55e",   # green
        "sleeping":     "#eab308",   # yellow
        "transcribing": "#eab308",   # yellow
    }
    colour = colour_map.get(state, "#6b7280")  # grey fallback
    if colour not in _icons:
        _icons[colour] = _make_icon(colour)
    return _icons[colour]


# ── Status file helpers ──────────────────────────────────────────────────────

def _read_status() -> dict:
    """Read the status file, return {} on failure."""
    try:
        if _STATUS_FILE.exists():
            data = json.loads(_STATUS_FILE.read_text())
            # Check staleness
            ts = data.get("timestamp", 0)
            if time.time() - ts > _STALE_THRESHOLD:
                return {}
            return data
    except Exception:
        pass
    return {}


# ── Port check ───────────────────────────────────────────────────────────────

def _is_port_in_use(port: int = _PORT) -> bool:
    """Return True if something is already listening on *port*."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── Streamlit subprocess management ─────────────────────────────────────────

class _StreamlitProcess:
    """Wraps the Streamlit subprocess."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        """Launch ``streamlit run app.py`` in the project directory."""
        app_dir = Path(__file__).resolve().parent
        app_py = app_dir / "app.py"

        # Use the same Python that's running this launcher
        python = sys.executable

        self._proc = subprocess.Popen(
            [
                python, "-m", "streamlit", "run", str(app_py),
                "--server.headless", "true",
                "--server.port", str(_PORT),
            ],
            cwd=str(app_dir),
            # On Windows, CREATE_NO_WINDOW prevents a visible console
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("Streamlit started (PID %s)", self._proc.pid)

    def stop(self) -> None:
        """Terminate the Streamlit process."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        logger.info("Streamlit stopped")
        self._proc = None

    @property
    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None


# ── Tray application ────────────────────────────────────────────────────────

class ThothTray:
    """System-tray icon that manages the Streamlit server."""

    def __init__(self) -> None:
        import pystray

        self._server = _StreamlitProcess()
        self._owns_server = False          # True if *we* started it
        self._stop_event = threading.Event()

        menu = pystray.Menu(
            pystray.MenuItem("Open Thoth", self._on_open, default=True),
            pystray.MenuItem("Quit", self._on_quit),
        )
        self._icon = pystray.Icon(
            name="Thoth",
            icon=_get_icon("stopped"),
            title="Thoth — stopped",
            menu=menu,
        )

    # ── Menu callbacks ───────────────────────────────────────────────────

    def _on_open(self, icon=None, item=None) -> None:   # noqa: ARG002
        webbrowser.open(_URL)

    def _on_quit(self, icon=None, item=None) -> None:    # noqa: ARG002
        logger.info("Quit requested")
        self._stop_event.set()
        if self._owns_server:
            self._server.stop()
        # Clean up status file
        try:
            _STATUS_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self._icon.stop()

    # ── Background poller ────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Periodically read status.json and update the tray icon."""
        while not self._stop_event.is_set():
            status = _read_status()
            voice_state = status.get("state", "stopped")
            voice_enabled = status.get("voice_enabled", False)

            # Update icon
            self._icon.icon = _get_icon(voice_state)

            # Update tooltip
            state_label = {
                "sleeping":     "💤 Waiting for wake word",
                "listening":    "🎙️ Listening…",
                "transcribing": "⏳ Transcribing…",
            }.get(voice_state, "Voice off" if not voice_enabled else "Stopped")
            self._icon.title = f"Thoth — {state_label}"

            self._stop_event.wait(_POLL_INTERVAL)

    # ── Entry point ──────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the tray icon and (if needed) the Streamlit server."""
        already_running = _is_port_in_use(_PORT)

        if already_running:
            logger.info("Streamlit already running on port %s", _PORT)
        else:
            self._server.start()
            self._owns_server = True
            # Register cleanup in case launcher crashes
            atexit.register(self._server.stop)

        # Start the status-polling thread
        poller = threading.Thread(target=self._poll_loop, daemon=True, name="tray-poll")
        poller.start()

        # Give Streamlit a moment to spin up, then open the browser
        def _delayed_open():
            if not already_running:
                deadline = time.monotonic() + _STARTUP_GRACE
                while time.monotonic() < deadline:
                    if _is_port_in_use(_PORT):
                        break
                    time.sleep(0.5)
            webbrowser.open(_URL)

        threading.Thread(target=_delayed_open, daemon=True).start()

        # Blocking — runs the tray icon's event loop on the main thread
        logger.info("Thoth tray running  (Ctrl+C or Quit menu to exit)")
        self._icon.run()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        tray = ThothTray()
        tray.run()
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
