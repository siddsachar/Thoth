"""Thoth Launcher — system-tray process that manages the NiceGUI server.

Responsibilities:
    • Splash screen while the server starts (tkinter — no extra deps)
    • System-tray icon (green = running, grey = stopped)
    • Launch  ``python app_nicegui.py``  as a managed subprocess
    • Open the browser to http://localhost:8080
    • Detect an already-running instance and just open the browser
    • Graceful shutdown on Quit
"""

from __future__ import annotations

import atexit
import logging
import os
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
_PORT = 8080
_URL = f"http://localhost:{_PORT}"
_STARTUP_GRACE = 15           # seconds to wait for NiceGUI before opening browser
_ICON_SIZE = 64               # px for generated tray icons


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
    """Return the icon for a launcher state string."""
    colour_map = {
        "running":  "#22c55e",   # green
    }
    colour = colour_map.get(state, "#6b7280")  # grey fallback
    if colour not in _icons:
        _icons[colour] = _make_icon(colour)
    return _icons[colour]


# ── Port check ───────────────────────────────────────────────────────────────

def _is_port_in_use(port: int = _PORT) -> bool:
    """Return True if something is already listening on *port*."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── NiceGUI subprocess management ───────────────────────────────────────────

class _ThothProcess:
    """Wraps the NiceGUI app subprocess."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._log_file: Path | None = None

    def start(self, *, native: bool = True) -> None:
        """Launch ``python app_nicegui.py`` in the project directory.

        If *native* is True (default) the app opens in a pywebview
        native OS window instead of a browser tab.
        """
        app_dir = Path(__file__).resolve().parent
        app_py = app_dir / "app_nicegui.py"

        # Use the same Python that's running this launcher
        python = sys.executable

        cmd = [python, str(app_py)]
        if native:
            cmd.append("--native")

        # Log file for diagnosing startup crashes —
        # lives in  ~/.thoth/thoth_app.log
        log_dir = Path.home() / ".thoth"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = log_dir / "thoth_app.log"
        log_fh = open(self._log_file, "w", encoding="utf-8")  # noqa: SIM115

        # Isolate from any system-wide Python site-packages
        # Force UTF-8 I/O so emoji in print() never crash on cp1252 consoles
        env = {**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8"}

        self._proc = subprocess.Popen(
            cmd,
            cwd=str(app_dir),
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            # On Windows, CREATE_NO_WINDOW prevents a visible console
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("Thoth started (PID %s, native=%s, log=%s)",
                     self._proc.pid, native, self._log_file)

    def stop(self) -> None:
        """Terminate the NiceGUI process."""
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
        logger.info("Thoth stopped")
        self._proc = None

    @property
    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None


# ── Splash screen (subprocess to avoid Tcl/pystray conflicts) ────────────────

# Tkinter GUI splash — tried first.
_SPLASH_TK = r'''
import os, sys, socket, time

py_dir = os.path.dirname(sys.executable)
os.environ['PATH'] = py_dir + os.pathsep + os.environ.get('PATH', '')
if os.name == 'nt':
    if hasattr(os, 'add_dll_directory'):
        os.add_dll_directory(py_dir)
    for d in ('tcl/tcl8.6', 'tcl/tk8.6'):
        p = os.path.join(py_dir, d)
        if os.path.isdir(p):
            os.environ['TCL_LIBRARY' if 'tcl8' in d else 'TK_LIBRARY'] = p
    import ctypes
    for dll in ('tcl86t.dll', 'tk86t.dll'):
        p = os.path.join(py_dir, dll)
        if os.path.exists(p):
            try: ctypes.CDLL(p, winmode=0)
            except OSError: pass
import tkinter as tk

PORT, TIMEOUT = int(sys.argv[1]), float(sys.argv[2])
def port_ready():
    try:
        s = socket.socket(); s.settimeout(0.3)
        s.connect(("127.0.0.1", PORT)); s.close(); return True
    except OSError: return False

BG, GOLD = "#1e1e1e", "#FFD700"
root = tk.Tk(); root.overrideredirect(True); root.attributes("-topmost", True)
root.configure(bg=BG)
sx, sy = root.winfo_screenwidth(), root.winfo_screenheight()
root.geometry(f"500x300+{(sx-500)//2}+{(sy-300)//2}")
tk.Label(root, text="\U0001305F", font=("Segoe UI Emoji", 64), fg=GOLD, bg=BG).pack(pady=(40,0))
tk.Label(root, text="Thoth", font=("Segoe UI", 28, "bold"), fg=GOLD, bg=BG).pack(pady=(0,10))
lbl = tk.Label(root, text="Loading.", font=("Segoe UI", 12), fg="#aaaaaa", bg=BG); lbl.pack()
_start, _d = time.monotonic(), [0]
def _check():
    _d[0] = (_d[0] % 3) + 1; lbl.configure(text="Loading" + "." * _d[0])
    if time.monotonic() - _start > TIMEOUT or port_ready(): root.destroy(); return
    root.after(500, _check)
root.after(500, _check); root.mainloop()
'''

# Console fallback — used when tkinter is unavailable.
_SPLASH_CONSOLE = r'''
import sys, socket, time, os
PORT, TIMEOUT = int(sys.argv[1]), float(sys.argv[2])
if os.name == 'nt':
    os.system('title Thoth')
def port_ready():
    try:
        s = socket.socket(); s.settimeout(0.3)
        s.connect(("127.0.0.1", PORT)); s.close(); return True
    except OSError: return False
print("\n  Thoth — Starting...\n")
_start, _d = time.monotonic(), 0
while time.monotonic() - _start < TIMEOUT:
    if port_ready(): break
    _d = (_d % 3) + 1
    print(f"\r  Loading{'.' * _d}{'   '}", end="", flush=True)
    time.sleep(0.5)
print("\r  Ready!       ")
time.sleep(0.6)
'''


def _show_splash(port: int = _PORT, timeout: float = 60.0) -> subprocess.Popen | None:
    """Launch a splash screen subprocess.  Tries tkinter first; falls back
    to a simple console window if tkinter is unavailable."""
    log_dir = Path.home() / ".thoth"
    log_dir.mkdir(parents=True, exist_ok=True)
    splash_log = log_dir / "splash.log"

    try:
        # --- Attempt 1: tkinter GUI splash ---
        log_fh = open(splash_log, "w", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-c", _SPLASH_TK, str(port), str(timeout)],
            stdout=log_fh, stderr=log_fh,
        )
        time.sleep(0.5)
        if proc.poll() is None:
            return proc  # tkinter splash is running
        log_fh.close()
        err = splash_log.read_text(encoding="utf-8", errors="replace").strip()
        logger.info("Tkinter splash unavailable (%s), falling back to console", err or "exited")

        # --- Attempt 2: console fallback ---
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_CONSOLE
        proc = subprocess.Popen(
            [sys.executable, "-c", _SPLASH_CONSOLE, str(port), str(timeout)],
            creationflags=flags,
        )
        return proc
    except Exception as exc:
        logger.warning("Could not show splash screen: %s", exc)
        return None


# ── Tray application ────────────────────────────────────────────────────────

class ThothTray:
    """System-tray icon that manages the NiceGUI server."""

    def __init__(self) -> None:
        import pystray

        self._server = _ThothProcess()
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
        if self._owns_server:
            if not self._server.is_alive:
                # Native window was closed — restart the process to reopen it
                logger.info("Re-launching Thoth native window")
                self._server.start(native=True)
            # else: native window is already open, nothing to do
        else:
            # Someone else started the server (e.g. dev mode) — open browser
            webbrowser.open(_URL)

    def _on_quit(self, icon=None, item=None) -> None:    # noqa: ARG002
        logger.info("Quit requested")
        self._stop_event.set()
        if self._owns_server:
            self._server.stop()
        self._icon.stop()

    # ── Background poller ────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Periodically check if the app is still alive and update icon."""
        _POLL_INTERVAL = 3.0  # seconds
        _crash_logged = False
        while not self._stop_event.is_set():
            if self._owns_server and self._server.is_alive:
                self._icon.icon = _get_icon("running")
                self._icon.title = "Thoth — running"
                _crash_logged = False
            elif not self._owns_server and _is_port_in_use(_PORT):
                self._icon.icon = _get_icon("running")
                self._icon.title = "Thoth — running"
            else:
                self._icon.icon = _get_icon("stopped")
                self._icon.title = "Thoth — stopped"
                # Log once when the app process dies unexpectedly
                if self._owns_server and not _crash_logged:
                    _crash_logged = True
                    rc = (self._server._proc.returncode
                          if self._server._proc else "?")
                    log_path = self._server._log_file or "?"
                    logger.error(
                        "Thoth app exited (code %s). "
                        "Check %s for details.", rc, log_path)
                    # Show the last few lines of the log for quick diagnosis
                    if self._server._log_file and self._server._log_file.exists():
                        try:
                            tail = self._server._log_file.read_text(
                                encoding="utf-8", errors="replace"
                            ).strip().splitlines()[-10:]
                            if tail:
                                logger.error("--- last lines of log ---")
                                for line in tail:
                                    logger.error("  %s", line)
                                logger.error("--- end of log ---")
                        except Exception:
                            pass

            self._stop_event.wait(_POLL_INTERVAL)

    # ── Entry point ──────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the tray icon and (if needed) the NiceGUI server."""
        already_running = _is_port_in_use(_PORT)

        if already_running:
            logger.info("Thoth already running on port %s", _PORT)
        else:
            self._server.start()
            self._owns_server = True
            # Register cleanup in case launcher crashes
            atexit.register(self._server.stop)

            # Show splash screen while the server starts up
            _show_splash()

        # Start the status-polling thread
        poller = threading.Thread(target=self._poll_loop, daemon=True, name="tray-poll")
        poller.start()

        # In native mode, the pywebview window opens automatically.
        # Only open a browser if we didn't start the server (external instance).
        if already_running:
            webbrowser.open(_URL)

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
