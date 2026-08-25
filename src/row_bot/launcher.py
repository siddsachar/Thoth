"""Row-Bot launcher: system-tray process that manages the NiceGUI server.

Responsibilities:
    • Splash screen while the server starts (tkinter — no extra deps)
    • Branded system-tray icon with running/stopped state feedback
    • Launch  ``python app.py``  as a headless server subprocess
    • Open a pywebview native window pointing at the server
    • Closing the window keeps the server (and tasks/channels) alive
    • Detect an already-running instance and just open a window
    • Graceful shutdown on Quit
"""

from __future__ import annotations

import atexit
import argparse
from dataclasses import dataclass
import io
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from row_bot.app_port import (
    DEFAULT_APP_PORT,
    ROW_BOT_HOST_ENV,
    ROW_BOT_PORT_ENV,
    parse_app_port,
)
from row_bot.access.launcher_control import (
    LAUNCH_SECRET_ENV,
    LauncherControlServer,
)
from row_bot.access.access_routes import (
    AccessRouteConfigStore,
    resolve_listen_host,
)
from row_bot.brand import (
    APP_AUTO_START_OLLAMA_ENV,
    APP_BRAND_ACCENT,
    APP_DISPLAY_NAME,
    APP_NATIVE_ENV,
    APP_PING_ID,
    APP_STARTUP_TIMEOUT_ENV,
    DEFAULT_DATA_DIR_NAME,
    LAUNCHER_APP_LOG_FILENAME,
)
from row_bot.data_paths import (
    describe_data_paths,
    get_access_db_path,
    get_memory_db_path,
    get_tasks_db_path,
    get_threads_db_path,
    get_row_bot_data_dir,
)
from row_bot.runtime_paths import app_icon_path, app_root, static_dir

if TYPE_CHECKING:
    from PIL import Image as _PILImage

# ── Setup logging ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
_LAUNCH_STARTED = time.perf_counter()
_LAUNCH_FILE_LOGGING = False
_LAUNCH_SESSION_ID = os.environ.get("ROW_BOT_LAUNCH_SESSION_ID") or uuid.uuid4().hex
_EARLY_SPLASH_PROC: subprocess.Popen | None = None

# ── Constants ────────────────────────────────────────────────────────────────
_PORT = DEFAULT_APP_PORT
_OLLAMA_PORT = 11434  # Ollama default API port
_STARTUP_GRACE = 15  # seconds to wait for NiceGUI before opening browser
_STARTUP_TIMEOUT_ENV = APP_STARTUP_TIMEOUT_ENV
_ICON_SIZE = 64  # px for generated tray icons
_APP_ICON_PATH = app_icon_path()
_APP_GLYPH_PATH = static_dir() / "row_bot_glyph_256.png"
_APP_FAVICON_PATH = static_dir() / "favicon.ico"
_ACTIVE_TRAY: "RowBotTray | None" = None
_OLLAMA_AUTOSTART_ENV = APP_AUTO_START_OLLAMA_ENV
_GRACEFUL_SHUTDOWN_REQUEST_TIMEOUT = 3.0
_GRACEFUL_SHUTDOWN_EXIT_TIMEOUT = 30.0
_QUIT_WATCHDOG_TIMEOUT = 75.0
_LAUNCHER_STATE_FILENAME = "launcher_state.json"
_LEGACY_RUNTIME_ENV_VARS = frozenset(
    {
        "THOTH_AUTO_START_OLLAMA",
        "THOTH_DATA_DIR",
        "THOTH_HOST",
        "THOTH_NATIVE",
        "THOTH_PORT",
        "THOTH_STARTUP_TIMEOUT",
        "THOTH_WEBVIEW_STORAGE_PATH",
    }
)


# ── Ollama auto-start ────────────────────────────────────────────────────────


def _is_ollama_running() -> bool:
    """Return True if Ollama's API is reachable on its default port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", _OLLAMA_PORT)) == 0


def _row_bot_data_dir() -> Path:
    return get_row_bot_data_dir()


def _ensure_launcher_file_logging() -> None:
    global _LAUNCH_FILE_LOGGING
    if _LAUNCH_FILE_LOGGING:
        return
    try:
        log_dir = _row_bot_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "launcher.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        _LAUNCH_FILE_LOGGING = True
    except Exception:
        logger.debug("Could not enable launcher file logging", exc_info=True)


def _launch_event(event: str, **fields) -> None:
    _ensure_launcher_file_logging()
    elapsed_ms = (time.perf_counter() - _LAUNCH_STARTED) * 1000.0
    trace = os.environ.get("ROW_BOT_LAUNCH_TRACE") == "1"
    compact = {
        key: value
        for key, value in fields.items()
        if trace
        or key
        in {
            "port",
            "host",
            "pid",
            "mode",
            "duration_ms",
            "exit_code",
            "status",
            "session",
            "log",
            "backend",
            "requested",
            "selected",
            "visible",
            "reason",
            "control_port",
            "window_pid",
        }
    }
    compact.setdefault("session", _LAUNCH_SESSION_ID)
    suffix = " ".join(f"{key}={value}" for key, value in compact.items())
    logger.info(
        "launcher.%s elapsed_ms=%.1f%s%s",
        event,
        elapsed_ms,
        " " if suffix else "",
        suffix,
    )


def _read_json_file(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("Could not read launcher settings from %s", path, exc_info=True)
    return {}


def _launcher_state_path() -> Path:
    return _row_bot_data_dir() / _LAUNCHER_STATE_FILENAME


def _write_launcher_state(
    *,
    port: int,
    mode: str,
    owns_server: bool,
    window_control_port: int | None = None,
    window_pid: int | None = None,
) -> None:
    path = _launcher_state_path()
    payload = {
        "app": APP_PING_ID,
        "pid": os.getpid(),
        "port": int(port),
        "mode": mode,
        "owns_server": bool(owns_server),
        "session": _LAUNCH_SESSION_ID,
        "updated_at": time.time(),
    }
    if window_control_port:
        payload["window_control_port"] = int(window_control_port)
    if window_pid:
        payload["window_pid"] = int(window_pid)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        _launch_event(
            "launcher_state_written",
            port=port,
            mode=mode,
            control_port=int(window_control_port or 0),
            window_pid=int(window_pid or 0),
        )
    except Exception:
        logger.debug("Could not write launcher state to %s", path, exc_info=True)


def _clear_launcher_state() -> None:
    path = _launcher_state_path()
    state = _read_json_file(path)
    if state and state.get("session") not in {None, _LAUNCH_SESSION_ID}:
        return
    try:
        path.unlink(missing_ok=True)
        _launch_event("launcher_state_cleared")
    except Exception:
        logger.debug("Could not clear launcher state at %s", path, exc_info=True)


def _model_ref_requires_ollama(model_name: str | None) -> bool:
    """Return True when a saved model selection needs the Ollama daemon.

    This is intentionally lightweight and import-free: the launcher must not
    construct provider/model runtime state just to decide whether to start.
    """
    value = str(model_name or "").strip()
    if not value:
        return False

    if value.startswith("model:"):
        parts = value.split(":", 2)
        if len(parts) >= 3:
            provider_id = parts[1].strip().lower()
            return provider_id in {"ollama", "local"}
        return False

    lowered = value.lower()
    if lowered.startswith("gpt-oss"):
        return True

    cloud_prefixes = (
        "gpt-",
        "o1",
        "o3",
        "o4",
        "chatgpt-",
        "claude",
        "gemini",
        "grok",
        "minimax",
    )
    if lowered.startswith(cloud_prefixes):
        return False

    cloud_provider_slugs = (
        "openai/",
        "anthropic/",
        "google/",
        "google-ai/",
        "xai/",
        "x-ai/",
        "minimax/",
        "openrouter/",
        "codex/",
        "chatgpt/",
    )
    if lowered.startswith(cloud_provider_slugs):
        return False

    # Bare legacy selections are local Ollama model names.
    return True


def _should_auto_start_ollama() -> bool:
    """Return True when persisted settings indicate an Ollama runtime is active."""
    override = os.environ.get(_OLLAMA_AUTOSTART_ENV)
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}

    data_dir = _row_bot_data_dir()
    model_settings = _read_json_file(data_dir / "model_settings.json")
    if _model_ref_requires_ollama(model_settings.get("model")):
        return True

    vision_settings_path = data_dir / "vision_settings.json"
    if vision_settings_path.exists():
        vision_settings = _read_json_file(vision_settings_path)
        if _model_ref_requires_ollama(vision_settings.get("model")):
            return True

    return False


def _maybe_start_ollama(*, no_ollama: bool = False) -> None:
    """Best-effort Ollama startup, only when a saved local runtime needs it."""
    if no_ollama:
        logger.info("Skipping Ollama auto-start (--no-ollama)")
        return
    if not _should_auto_start_ollama():
        logger.info(
            "Skipping Ollama auto-start; no saved local Ollama runtime selected"
        )
        return
    _start_ollama()


def _start_ollama() -> None:
    """Auto-start Ollama if it is installed but not already running."""
    if _is_ollama_running():
        logger.info("Ollama already running on port %s", _OLLAMA_PORT)
        return

    # --- Windows: prefer the GUI app (tray icon) for better UX ---
    if sys.platform == "win32":
        app_exe = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "Ollama"
            / "ollama app.exe"
        )
        if app_exe.exists():
            logger.info("Starting Ollama (Windows app)...")
            subprocess.Popen(
                [str(app_exe)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _wait_for_ollama()
            return

    # --- macOS / Linux (and Windows fallback): use `ollama serve` ---
    ollama_bin = shutil.which("ollama")
    if ollama_bin is None:
        # macOS: Homebrew may not be on PATH in a GUI launch context
        for candidate in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"):
            if os.path.isfile(candidate):
                ollama_bin = candidate
                break

    if ollama_bin is None:
        logger.warning(
            "Ollama not found — install it from https://ollama.com/download  "
            "Ollama is only needed when using local Ollama models."
        )
        return

    logger.info("Starting Ollama (%s)...", ollama_bin)
    subprocess.Popen(
        [ollama_bin, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach from launcher's process group
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _wait_for_ollama()


def _wait_for_ollama(timeout: float = 15.0) -> None:
    """Block until Ollama's API port is reachable, or until *timeout*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_ollama_running():
            logger.info("Ollama is ready")
            return
        time.sleep(0.5)
    logger.warning("Ollama did not become reachable within %.0fs", timeout)


# ── Tray icon generation (branded Pillow image for pystray) ──────────────────


def _make_status_dot_icon(colour: str) -> _PILImage.Image:
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


def _tray_icon_candidate_paths() -> tuple[Path, ...]:
    return (_APP_GLYPH_PATH, _APP_ICON_PATH, _APP_FAVICON_PATH)


def _normalize_tray_icon(image: _PILImage.Image) -> _PILImage.Image:
    from PIL import Image

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    icon = image.convert("RGBA")
    alpha_bbox = icon.getchannel("A").getbbox()
    if alpha_bbox:
        icon = icon.crop(alpha_bbox)
    icon.thumbnail((_ICON_SIZE, _ICON_SIZE), resampling)
    canvas = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    left = (_ICON_SIZE - icon.width) // 2
    top = (_ICON_SIZE - icon.height) // 2
    canvas.alpha_composite(icon, (left, top))
    return canvas


_tray_base_icon_loaded = False
_tray_base_icon: _PILImage.Image | None = None
_icons: dict[str, _PILImage.Image] = {}


def _load_tray_base_icon() -> _PILImage.Image | None:
    """Load the branded tray base icon, falling back gracefully on failures."""
    global _tray_base_icon_loaded, _tray_base_icon
    if _tray_base_icon_loaded:
        return _tray_base_icon

    from PIL import Image

    _tray_base_icon_loaded = True
    for path in _tray_icon_candidate_paths():
        try:
            if not path or not Path(path).exists():
                continue
            with Image.open(path) as image:
                _tray_base_icon = _normalize_tray_icon(image)
                return _tray_base_icon
        except Exception:
            logger.debug("Could not load tray icon asset %s", path, exc_info=True)
    _tray_base_icon = None
    return None


def _draw_status_badge(image: _PILImage.Image, state: str) -> None:
    from PIL import ImageDraw

    colour = "#22c55e" if state == "running" else "#6b7280"
    draw = ImageDraw.Draw(image)
    badge_size = max(14, _ICON_SIZE // 4)
    margin = max(3, _ICON_SIZE // 16)
    left = _ICON_SIZE - badge_size - margin
    top = _ICON_SIZE - badge_size - margin
    bbox = [left, top, left + badge_size, top + badge_size]
    outline_width = max(2, _ICON_SIZE // 32)
    draw.ellipse(bbox, fill=colour, outline=(15, 23, 42, 230), width=outline_width)


def _use_branded_tray_icon() -> bool:
    if sys.platform == "darwin":
        return os.environ.get("ROW_BOT_BRANDED_TRAY_ICON", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return True


def _get_icon(state: str) -> _PILImage.Image:
    """Return the icon for a launcher state string."""
    cache_key = "running" if state == "running" else "stopped"
    if cache_key not in _icons:
        base_icon = _load_tray_base_icon() if _use_branded_tray_icon() else None
        if base_icon is not None:
            icon = base_icon.copy()
            _draw_status_badge(icon, cache_key)
        else:
            icon = _make_status_dot_icon(
                "#22c55e" if cache_key == "running" else "#6b7280"
            )
        _icons[cache_key] = icon
    return _icons[cache_key]


@dataclass(frozen=True)
class _TrayMenuEntry:
    label: str = ""
    callback: Callable[..., None] | None = None
    default: bool = False
    separator: bool = False


def _tray_separator() -> _TrayMenuEntry:
    return _TrayMenuEntry(separator=True)


def _build_tray_menu_entries(tray: "RowBotTray") -> tuple[_TrayMenuEntry, ...]:
    return (
        _TrayMenuEntry(f"Open {APP_DISPLAY_NAME}", tray._on_open, default=True),
        _TrayMenuEntry("Open in Browser", tray._on_open_browser),
        _tray_separator(),
        _TrayMenuEntry("Show Buddy", tray._on_show_buddy),
        _TrayMenuEntry("Hide Buddy", tray._on_hide_buddy),
        _tray_separator(),
        _TrayMenuEntry("Quit", tray._on_quit),
    )


class _PystrayTrayBackend:
    """Thin wrapper around pystray so platform-specific backends share an API."""

    def __init__(self, menu_entries: tuple[_TrayMenuEntry, ...]) -> None:
        import pystray

        self.backend_name = "pystray"
        self._pystray = pystray
        self._icon = pystray.Icon(
            name=APP_DISPLAY_NAME,
            icon=_get_icon("stopped"),
            title=f"{APP_DISPLAY_NAME} - stopped",
            menu=self._build_menu(menu_entries),
        )
        _launch_event("tray_backend_ready", backend=self.backend_name)

    def _build_menu(self, menu_entries: tuple[_TrayMenuEntry, ...]):
        items = []
        for entry in menu_entries:
            if entry.separator:
                items.append(self._pystray.Menu.SEPARATOR)
            elif entry.callback is not None:
                items.append(
                    self._pystray.MenuItem(
                        entry.label,
                        entry.callback,
                        default=entry.default,
                    )
                )
        return self._pystray.Menu(*items)

    @property
    def icon(self):
        return self._icon.icon

    @icon.setter
    def icon(self, value) -> None:
        self._icon.icon = value

    @property
    def title(self) -> str:
        return self._icon.title

    @title.setter
    def title(self, value: str) -> None:
        self._icon.title = value

    @property
    def visible(self) -> bool:
        return self._icon.visible

    @visible.setter
    def visible(self, value: bool) -> None:
        self._icon.visible = value

    def run(self, setup: Callable[[object], None] | None = None) -> None:
        if sys.platform == "darwin" and setup is not None:

            def _setup(_icon) -> None:
                self.visible = True
                _launch_event(
                    "pystray_visible_set", backend=self.backend_name, visible=True
                )
                setup(self)

            self._icon.run(setup=_setup)
            return
        self._icon.run(setup=setup)

    def stop(self) -> None:
        self._icon.stop()


_MAC_MENU_ITEM_SELECTOR = b"activateMenuItem:"
_MAC_STATUS_ITEM_DELEGATE_CLASS = None
_MAC_STATUS_ITEM_FALLBACK_TITLE = "RB"
_MAC_STATUS_ITEM_MIN_LENGTH = 44.0


def _mac_status_item_delegate_class(Foundation, objc):
    global _MAC_STATUS_ITEM_DELEGATE_CLASS
    if _MAC_STATUS_ITEM_DELEGATE_CLASS is not None:
        return _MAC_STATUS_ITEM_DELEGATE_CLASS

    class _MacStatusItemDelegate(Foundation.NSObject):
        @objc.namedSelector(_MAC_MENU_ITEM_SELECTOR)
        def activate_menu_item(self, sender):
            self.backend._activate_menu_item(sender)

    _MAC_STATUS_ITEM_DELEGATE_CLASS = _MacStatusItemDelegate
    return _MAC_STATUS_ITEM_DELEGATE_CLASS


class _MacStatusItemBackend:
    """Native macOS menu-bar backend using AppKit NSStatusItem."""

    def __init__(self, menu_entries: tuple[_TrayMenuEntry, ...]) -> None:
        try:
            import AppKit
            import Foundation
            import objc
            from PyObjCTools import AppHelper
        except ImportError:
            raise
        except Exception as exc:
            raise ImportError(
                f"could not import macOS AppKit tray dependencies: {exc}"
            ) from exc

        self.backend_name = "appkit"
        self._AppKit = AppKit
        self._Foundation = Foundation
        self._AppHelper = AppHelper
        self._app = AppKit.NSApplication.sharedApplication()
        self._status_bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = self._status_bar.statusItemWithLength_(
            _MAC_STATUS_ITEM_MIN_LENGTH
        )
        if self._status_item is None:
            raise RuntimeError("AppKit did not return an NSStatusItem")
        self._button = self._status_item.button()
        if self._button is None:
            raise RuntimeError("AppKit status item has no button")

        delegate_class = _mac_status_item_delegate_class(Foundation, objc)
        self._delegate = delegate_class.alloc().init()
        self._delegate.backend = self
        self._callbacks: list[Callable[..., None]] = []
        self._menu = self._create_menu(menu_entries)
        self._nsimage = None
        self._icon = _get_icon("stopped")
        self._title = f"{APP_DISPLAY_NAME} - stopped"

        try:
            activation_policy = getattr(
                AppKit, "NSApplicationActivationPolicyAccessory", 1
            )
            self._app.setActivationPolicy_(activation_policy)
        except Exception:
            logger.debug("Could not set macOS tray activation policy", exc_info=True)

        self._status_item.setMenu_(self._menu)
        self._set_status_item_length_on_main()
        self._set_button_fallback_title_on_main()
        self._set_icon_on_main(self._icon)
        self._set_title_on_main(self._title)
        self._set_visible_on_main(False)
        _launch_event(
            "tray_status_item_created",
            backend=self.backend_name,
            visible=False,
            status="text_fallback",
        )
        _launch_event("tray_backend_ready", backend=self.backend_name)

    def _create_menu(self, menu_entries: tuple[_TrayMenuEntry, ...]):
        menu = self._AppKit.NSMenu.alloc().initWithTitle_(APP_DISPLAY_NAME)
        if hasattr(menu, "setAutoenablesItems_"):
            menu.setAutoenablesItems_(False)
        for entry in menu_entries:
            if entry.separator:
                item = self._AppKit.NSMenuItem.separatorItem()
            else:
                item = (
                    self._AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        entry.label,
                        _MAC_MENU_ITEM_SELECTOR,
                        "",
                    )
                )
                item.setTarget_(self._delegate)
                item.setTag_(len(self._callbacks))
                item.setEnabled_(entry.callback is not None)
                if entry.callback is not None:
                    self._callbacks.append(entry.callback)
            menu.addItem_(item)
        return menu

    def _activate_menu_item(self, sender) -> None:
        tag = int(sender.tag())
        callback = self._callbacks[tag]
        callback(self, sender)

    def _schedule_on_main(self, func: Callable[..., None], *args) -> None:
        if threading.current_thread() is threading.main_thread():
            func(*args)
            return
        self._AppHelper.callAfter(func, *args)

    def _status_icon_size(self) -> tuple[int, int]:
        try:
            thickness = int(round(float(self._status_bar.thickness())))
        except Exception:
            thickness = 22
        thickness = max(18, min(32, thickness))
        return (thickness, thickness)

    def _status_item_length(self) -> float:
        width, _height = self._status_icon_size()
        return max(_MAC_STATUS_ITEM_MIN_LENGTH, float(width + 22))

    def _to_nsimage(self, image):
        from PIL import Image as PILImage

        size = self._status_icon_size()
        source = image.convert("RGBA")
        if source.size != size:
            source = source.resize(size, PILImage.LANCZOS)
        buffer = io.BytesIO()
        source.save(buffer, "png")
        data = self._Foundation.NSData(buffer.getvalue())
        return self._AppKit.NSImage.alloc().initWithData_(data)

    def _set_icon_on_main(self, image) -> None:
        self._nsimage = self._to_nsimage(image)
        self._button.setImage_(self._nsimage)
        if hasattr(self._button, "setImagePosition_"):
            image_left = getattr(self._AppKit, "NSImageLeft", 2)
            self._button.setImagePosition_(image_left)

    def _set_button_fallback_title_on_main(self) -> None:
        if hasattr(self._button, "setTitle_"):
            self._button.setTitle_(_MAC_STATUS_ITEM_FALLBACK_TITLE)

    def _set_status_item_length_on_main(self) -> None:
        if hasattr(self._status_item, "setLength_"):
            self._status_item.setLength_(self._status_item_length())

    def _set_title_on_main(self, value: str) -> None:
        if hasattr(self._button, "setToolTip_"):
            self._button.setToolTip_(value)
        elif hasattr(self._status_item, "setToolTip_"):
            self._status_item.setToolTip_(value)

    def _set_visible_on_main(self, visible: bool) -> None:
        if hasattr(self._status_item, "setVisible_"):
            self._status_item.setVisible_(visible)
        elif hasattr(self._button, "setHidden_"):
            self._button.setHidden_(not visible)

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value) -> None:
        self._icon = value
        self._schedule_on_main(self._set_icon_on_main, value)

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value
        self._schedule_on_main(self._set_title_on_main, value)

    def run(self, setup: Callable[[object], None] | None = None) -> None:
        self._set_status_item_length_on_main()
        self._set_button_fallback_title_on_main()
        self._set_visible_on_main(True)
        _launch_event(
            "tray_status_item_visible", backend=self.backend_name, visible=True
        )
        if setup is not None:
            threading.Thread(
                target=lambda: setup(self), daemon=True, name="mac-tray-setup"
            ).start()
        self._app.run()

    def stop(self) -> None:
        self._schedule_on_main(self._stop_on_main)

    def _stop_on_main(self) -> None:
        if self._status_item is not None:
            try:
                self._status_bar.removeStatusItem_(self._status_item)
            except Exception:
                logger.debug("Could not remove macOS status item", exc_info=True)
            self._status_item = None
        try:
            self._app.stop_(self._app)
        except Exception:
            logger.debug("Could not stop macOS AppKit loop", exc_info=True)
        self._post_stop_event()

    def _post_stop_event(self) -> None:
        try:
            event_factory = getattr(
                self._AppKit.NSEvent,
                "otherEventWithType_"
                "location_"
                "modifierFlags_"
                "timestamp_"
                "windowNumber_"
                "context_"
                "subtype_"
                "data1_"
                "data2_",
            )
            event = event_factory(
                self._AppKit.NSApplicationDefined,
                self._AppKit.NSPoint(0, 0),
                0,
                0.0,
                0,
                None,
                0,
                0,
                0,
            )
            self._app.postEvent_atStart_(event, False)
        except Exception:
            logger.debug("Could not post macOS AppKit stop event", exc_info=True)


def _mac_tray_backend_request() -> str:
    requested = os.environ.get("ROW_BOT_MAC_TRAY_BACKEND", "auto").strip().lower()
    if requested in {"", "auto"}:
        return "auto"
    if requested in {"native", "appkit", "nsstatusitem"}:
        return "appkit"
    if requested == "pystray":
        return "pystray"
    logger.warning("Unknown ROW_BOT_MAC_TRAY_BACKEND=%r; using auto", requested)
    return "auto"


def _create_tray_backend(menu_entries: tuple[_TrayMenuEntry, ...]):
    if sys.platform == "darwin":
        requested = _mac_tray_backend_request()
        selected = "pystray" if requested == "pystray" else "appkit"
        _launch_event("tray_backend_select", requested=requested, selected=selected)
        if requested != "pystray":
            try:
                return _MacStatusItemBackend(menu_entries)
            except Exception as exc:
                logger.warning(
                    "Native macOS tray backend unavailable; falling back to pystray: %s",
                    exc,
                )
                if isinstance(exc, ImportError):
                    _launch_event(
                        "tray_backend_import_failed",
                        backend="appkit",
                        requested=requested,
                        reason=type(exc).__name__,
                    )
                _launch_event(
                    "tray_backend_fallback",
                    backend="pystray",
                    requested=requested,
                    reason=type(exc).__name__,
                )
        return _PystrayTrayBackend(menu_entries)

    _launch_event("tray_backend_select", requested="pystray", selected="pystray")
    return _PystrayTrayBackend(menu_entries)


# ── Port check ───────────────────────────────────────────────────────────────


def _is_port_in_use(port: int = _PORT) -> bool:
    """Return True if something is already listening on *port*."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _url_for_port(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _resolve_launch_host(cli_host: object) -> str:
    """Resolve CLI > environment > durable Remote Access > safe default."""
    explicit = str(cli_host).strip() if cli_host is not None else ""
    return resolve_listen_host(
        explicit_host=explicit or None,
        environ=os.environ,
        config=AccessRouteConfigStore().load_or_default(),
    ).host


def _has_display_server() -> bool:
    """Return True when a Linux/Unix GUI display appears available."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _is_row_bot_server(
    port: int,
    timeout: float = 0.25,
    *,
    launch_secret: str | None = None,
) -> bool:
    """Return True if *port* is serving this app."""
    url = f"http://127.0.0.1:{port}/api/launcher-ping"
    headers = {"Authorization": f"Bearer {launch_secret}"} if launch_secret else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return False
            data = response.read(512).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, TimeoutError):
        return False
    return f'"app":"{APP_PING_ID}"' in data.replace(" ", "").lower()


def _find_existing_row_bot_port(start: int = _PORT, max_tries: int = 50) -> int | None:
    """Return an already-running Row-Bot port in the session range, if any."""
    for port in range(start, start + max_tries):
        if _is_port_in_use(port) and _is_row_bot_server(port):
            return port
    return None


def _find_free_port(start: int = _PORT, max_tries: int = 50) -> int:
    """Return the first free localhost port at or above *start*."""
    for port in range(start, start + max_tries):
        if not _is_port_in_use(port):
            return port
    raise RuntimeError(
        f"No free {APP_DISPLAY_NAME} app port found in {start}-{start + max_tries - 1}"
    )


def _startup_failure_hints(
    log_text: str, python_executable: str | None = None
) -> list[str]:
    """Return user-actionable recovery hints for known startup crashes."""
    text = (log_text or "").lower()
    python = python_executable or sys.executable
    hints: list[str] = []
    if "torchcodec" in text and (
        "libtorchcodec" in text
        or "could not load this library" in text
        or "could not find module" in text
    ):
        site_packages = Path(python).resolve().parent / "Lib" / "site-packages"
        hints.extend(
            [
                f"Detected a broken optional TorchCodec install in {APP_DISPLAY_NAME}'s embedded Python.",
                f"{APP_DISPLAY_NAME} does not require TorchCodec for built-in TTS.",
                f'Recovery: close {APP_DISPLAY_NAME} and run "{python}" -m pip uninstall -y torchcodec',
                f"If pip cannot remove it, delete torchcodec and torchcodec-*.dist-info from {site_packages}.",
            ]
        )
    if (
        "cv2" in text
        or "opencv" in text
        or "libgl.so" in text
        or "libglib" in text
        or "libgthread" in text
        or "xcb" in text
    ):
        hints.extend(
            [
                "Detected a likely OpenCV/Linux native dependency failure during startup.",
                f"Camera and screenshot capture are optional; {APP_DISPLAY_NAME} should still start without them after the Linux startup hardening fix.",
                "Recovery on Debian/Ubuntu: sudo apt-get install -y libgl1 libegl1 libglib2.0-0 libxcb-cursor0",
                f"Then restart {APP_DISPLAY_NAME} and check ~/{DEFAULT_DATA_DIR_NAME}/"
                f"{LAUNCHER_APP_LOG_FILENAME} if startup still fails.",
            ]
        )
    if (
        "faiss" in text
        and ("importerror" in text or "oserror" in text)
        and "successfully loaded faiss" not in text
    ):
        hints.extend(
            [
                "Detected a FAISS native import failure during startup.",
                f"Recovery: reinstall {APP_DISPLAY_NAME}'s packaged runtime or install the Linux libraries named in the traceback.",
            ]
        )
    if (
        "numpy.dtype size changed" in text
        or "numpy.core.multiarray failed to import" in text
        or "numpy was built with baseline optimizations" in text
        or "x86_v2" in text
    ):
        hints.extend(
            [
                "Detected a NumPy/native wheel startup failure.",
                "On older x86_64 CPUs this can happen if the packaged NumPy wheel requires x86-64-v2 instructions.",
                f"Recovery: install a {APP_DISPLAY_NAME} Linux build that pins NumPy below the x86-64-v2 wheel line, or rebuild the Linux tarball from this checkout.",
            ]
        )
    return hints


def _startup_timeout(default: float = 120.0) -> float:
    raw = os.environ.get(_STARTUP_TIMEOUT_ENV, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 5 else default


def _read_log_tail(log_path: Path | None, max_lines: int = 80) -> str:
    if not log_path or not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max(1, max_lines) :])


def _log_app_log_tail(log_path: Path | None, *, max_lines: int = 80) -> None:
    tail = _read_log_tail(log_path, max_lines=max_lines)
    if not tail:
        logger.error("No app startup log tail available at %s", log_path)
        return
    logger.error("--- last %d line(s) of %s ---", max_lines, log_path)
    for line in tail.splitlines():
        logger.error("  %s", line)
    logger.error("--- end app startup log tail ---")


def _log_startup_failure_context(
    server: "_RowBotProcess", port: int, reason: str
) -> None:
    exit_code = server.returncode
    logger.error(
        "%s server failed to become ready on port %s: %s",
        APP_DISPLAY_NAME,
        port,
        reason,
    )
    if exit_code is not None:
        logger.error("%s app process exited with code %s", APP_DISPLAY_NAME, exit_code)
    logger.error("Python executable: %s", sys.executable)
    logger.error("App log: %s", server._log_file)
    _log_app_log_tail(server._log_file)
    tail = _read_log_tail(server._log_file, max_lines=200)
    if tail:
        _log_startup_failure_hints(server._log_file, python_executable=sys.executable)


def _log_startup_failure_hints(
    log_path: Path | None, python_executable: str | None = None
) -> None:
    if not log_path or not log_path.exists():
        return
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    hints = _startup_failure_hints(text, python_executable=python_executable)
    if not hints:
        return
    logger.error("--- startup recovery hints ---")
    for hint in hints:
        logger.error("  %s", hint)
    logger.error("--- end recovery hints ---")


def _select_app_port(preferred: int = _PORT, max_tries: int = 50) -> tuple[int, bool]:
    """Choose the app port and whether it belongs to an existing Row-Bot."""
    if not _is_port_in_use(preferred):
        return preferred, False
    if _is_row_bot_server(preferred):
        return preferred, True
    for port in range(preferred + 1, preferred + max_tries):
        if not _is_port_in_use(port):
            return port, False
        if _is_row_bot_server(port):
            return port, True
    raise RuntimeError(
        f"No free {APP_DISPLAY_NAME} app port found in {preferred}-{preferred + max_tries - 1}"
    )


# ── NiceGUI subprocess management ───────────────────────────────────────────


def _set_process_launch_environment(
    process: object,
    values: dict[str, str],
) -> None:
    setter = getattr(process, "set_launch_environment", None)
    if callable(setter):
        setter(values)


class _RowBotProcess:
    """Wraps the NiceGUI app subprocess."""

    def __init__(self, port: int = _PORT, host: str | None = None) -> None:
        self._proc: subprocess.Popen | None = None
        self._log_file: Path | None = None
        self._log_handle = None
        self._launch_environment: dict[str, str] = {}
        self.port = port
        self.host = _resolve_launch_host(host)

    def set_launch_environment(self, values: dict[str, str]) -> None:
        """Set ephemeral launcher-to-child values that are never persisted."""
        self._launch_environment.update(
            {
                str(key): str(value)
                for key, value in values.items()
                if str(key) and str(value)
            }
        )

    def start(self, port: int | None = None, host: str | None = None) -> None:
        """Launch ``python app.py`` as a headless server.

        The server runs without ``--native`` so it stays alive
        independently of any UI window.  The launcher opens a
        separate pywebview window to display the UI.
        """
        app_dir = app_root()
        app_py = app_dir / "app.py"
        if port is not None:
            self.port = port
        if host is not None:
            self.host = _resolve_launch_host(host)

        # Use the same Python that's running this launcher
        python = sys.executable

        cmd = [python, str(app_py)]

        # Log file for diagnosing startup crashes.
        log_dir = _row_bot_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = log_dir / LAUNCHER_APP_LOG_FILENAME

        # Rotate previous log so crash evidence survives a restart
        if self._log_file.exists():
            prev = self._log_file.with_suffix(".log.prev")
            try:
                self._log_file.replace(prev)
                logger.info("Previous log rotated to %s", prev)
            except OSError as exc:
                logger.warning("Could not rotate log: %s", exc)

        self._close_log_handle()
        self._log_handle = open(self._log_file, "w", encoding="utf-8", buffering=1)  # noqa: SIM115

        # Isolate from any system-wide Python site-packages
        # Force UTF-8 I/O so emoji in print() never crash on cp1252 consoles
        # ROW_BOT_NATIVE=1 tells the app it's behind a pywebview window.
        # (used by _save_export to write to ~/Downloads on macOS WebKit)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in _LEGACY_RUNTIME_ENV_VARS
        }
        env.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONIOENCODING": "utf-8",
                "ROW_BOT_LAUNCH_SESSION_ID": _LAUNCH_SESSION_ID,
                APP_NATIVE_ENV: "1",
                ROW_BOT_PORT_ENV: str(self.port),
            }
        )
        env[ROW_BOT_HOST_ENV] = self.host
        env.update(self._launch_environment)

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(app_dir),
                env=env,
                stdout=self._log_handle,
                stderr=self._log_handle,
                # On Windows, CREATE_NO_WINDOW prevents a visible console
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self._close_log_handle()
            raise
        logger.info(
            "%s server started on %s:%s (PID %s, log=%s)",
            APP_DISPLAY_NAME,
            self.host,
            self.port,
            self._proc.pid,
            self._log_file,
        )

    def _close_log_handle(self) -> None:
        if self._log_handle is None:
            return
        try:
            self._log_handle.close()
        except Exception:
            logger.debug(
                "Could not close %s app log handle", APP_DISPLAY_NAME, exc_info=True
            )
        finally:
            self._log_handle = None

    def _request_graceful_shutdown(
        self, timeout: float = _GRACEFUL_SHUTDOWN_REQUEST_TIMEOUT
    ) -> bool:
        """Ask the app to run cleanup before the launcher falls back to killing it."""
        if self._proc is None or self._proc.poll() is not None:
            return True
        url = f"http://127.0.0.1:{self.port}/api/launcher-shutdown"
        secret = self._launch_environment.get(LAUNCH_SECRET_ENV, "")
        req = urllib.request.Request(
            url,
            data=b"",
            method="POST",
            headers={"Authorization": f"Bearer {secret}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                return 200 <= status < 300
        except (OSError, urllib.error.URLError, TimeoutError):
            return False

    @staticmethod
    def _terminate_process(
        proc: subprocess.Popen | None,
        *,
        label: str,
        timeout: float = 5.0,
        kill_tree: bool = False,
    ) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            logger.warning("%s did not exit after terminate; killing", label)
        except Exception:
            logger.debug("%s terminate failed; killing", label, exc_info=True)

        if kill_tree and sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                logger.debug("%s taskkill fallback failed", label, exc_info=True)
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
        except Exception:
            logger.debug("%s kill fallback failed", label, exc_info=True)

    def stop(self) -> None:
        """Terminate the NiceGUI process."""
        if self._proc is None:
            self._close_log_handle()
            return
        proc = self._proc
        stopped_gracefully = False
        shutdown_started = time.monotonic()
        try:
            if proc.poll() is None:
                graceful = self._request_graceful_shutdown()
                if graceful:
                    try:
                        proc.wait(timeout=_GRACEFUL_SHUTDOWN_EXIT_TIMEOUT)
                        stopped_gracefully = True
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            "%s graceful shutdown exceeded %.0fs; forcing stop",
                            APP_DISPLAY_NAME,
                            _GRACEFUL_SHUTDOWN_EXIT_TIMEOUT,
                        )
                    else:
                        logger.info(
                            "%s graceful shutdown completed in %.1fs",
                            APP_DISPLAY_NAME,
                            time.monotonic() - shutdown_started,
                        )
                else:
                    logger.warning(
                        "%s graceful shutdown request failed; forcing stop",
                        APP_DISPLAY_NAME,
                    )
                self._terminate_process(
                    proc,
                    label=f"{APP_DISPLAY_NAME} server",
                    timeout=5,
                    kill_tree=True,
                )
        finally:
            self._close_log_handle()
        logger.info(
            "%s stopped%s",
            APP_DISPLAY_NAME,
            " gracefully" if stopped_gracefully else "",
        )
        self._proc = None

    @property
    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def returncode(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()


def _helper_artifact_path(name: str, suffix: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
    return _row_bot_data_dir() / f"launcher-{_LAUNCH_SESSION_ID}-{safe}.{suffix}"


def _delete_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.debug(
            "Could not remove stale launcher helper artifact %s", path, exc_info=True
        )


def _launcher_helper_tail(log_path: Path, max_lines: int = 20) -> str:
    return _read_log_tail(log_path, max_lines=max_lines).replace("\n", " | ")[:1000]


def _start_launcher_helper(
    *,
    name: str,
    source: str,
    args: list[str],
    ready_marker: Path,
    log_path: Path,
    ready_timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.Popen | None:
    """Start a launcher UI helper and require an explicit ready marker."""
    _delete_if_exists(ready_marker)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-c", source, *args, str(ready_marker)]
    _launch_event(
        "helper_spawn",
        status=name,
        log=str(log_path),
        command=f"{Path(sys.executable).name} -c <{name}>",
    )
    try:
        with open(log_path, "w", encoding="utf-8") as log_fh:  # noqa: SIM115
            proc = subprocess.Popen(
                command,
                env=env,
                stdout=log_fh,
                stderr=log_fh,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except Exception as exc:
        logger.warning("Could not start %s helper: %s (log=%s)", name, exc, log_path)
        _launch_event("helper_spawn_failed", status=name, log=str(log_path))
        return None

    _launch_event("helper_started", status=name, pid=proc.pid, log=str(log_path))
    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        if ready_marker.exists():
            _launch_event("helper_ready", status=name, pid=proc.pid)
            return proc
        exit_code = proc.poll()
        if exit_code is not None:
            tail = _launcher_helper_tail(log_path)
            logger.warning(
                "%s helper exited before ready (code %s, log=%s): %s",
                name,
                exit_code,
                log_path,
                tail,
            )
            _launch_event(
                "helper_exited_before_ready",
                status=name,
                exit_code=exit_code,
                log=str(log_path),
            )
            return None
        time.sleep(0.05)

    logger.warning(
        "%s helper did not report ready within %.1fs; terminating (log=%s)",
        name,
        ready_timeout,
        log_path,
    )
    _launch_event(
        "helper_ready_timeout",
        status=name,
        pid=proc.pid,
        duration_ms=round(ready_timeout * 1000.0, 1),
        log=str(log_path),
    )
    _RowBotProcess._terminate_process(
        proc, label=f"{name} helper", timeout=1.0, kill_tree=True
    )
    return None


def _wait_for_launcher_helper(
    proc: subprocess.Popen,
    *,
    name: str,
    timeout: float,
    log_path: Path,
) -> int | None:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(
            "%s helper interaction timed out after %.1fs; terminating (log=%s)",
            name,
            timeout,
            log_path,
        )
        _launch_event(
            "helper_interaction_timeout",
            status=name,
            pid=proc.pid,
            duration_ms=round(timeout * 1000.0, 1),
            log=str(log_path),
        )
        _RowBotProcess._terminate_process(
            proc, label=f"{name} helper", timeout=1.0, kill_tree=True
        )
        return None


def _stop_launcher_helper(proc: subprocess.Popen | None, *, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    _RowBotProcess._terminate_process(
        proc, label=f"{name} helper", timeout=1.0, kill_tree=True
    )


def _claim_early_splash() -> subprocess.Popen | None:
    global _EARLY_SPLASH_PROC
    proc = _EARLY_SPLASH_PROC
    _EARLY_SPLASH_PROC = None
    return proc


# ── Splash screen (subprocess to avoid Tcl/pystray conflicts) ────────────────

# Tkinter GUI splash — tried first.
_SPLASH_TK = r"""
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
GLYPH_PATH = sys.argv[3] if len(sys.argv) > 3 else ""
ICON_PATH = sys.argv[4] if len(sys.argv) > 4 else ""
BRAND_BLUE = sys.argv[5] if len(sys.argv) > 5 else "#4F78A4"
READY_MARKER = sys.argv[6] if len(sys.argv) > 6 else ""
def port_ready():
    try:
        s = socket.socket(); s.settimeout(0.3)
        s.connect(("127.0.0.1", PORT)); s.close(); return True
    except OSError: return False

BG = "#1e1e1e"
root = tk.Tk(); root.overrideredirect(True); root.attributes("-topmost", True)
root.configure(bg=BG)
try:
    if ICON_PATH and os.path.isfile(ICON_PATH):
        root.iconbitmap(ICON_PATH)
except Exception:
    pass
sx, sy = root.winfo_screenwidth(), root.winfo_screenheight()
root.geometry(f"500x300+{(sx-500)//2}+{(sy-300)//2}")
try:
    logo = tk.PhotoImage(file=GLYPH_PATH)
    max_side = max(int(logo.width()), int(logo.height()), 1)
    factor = max(1, round(max_side / 128))
    if factor > 1:
        logo = logo.subsample(factor, factor)
    root._row_bot_logo = logo
    tk.Label(root, image=logo, bg=BG).pack(pady=(34,0))
except Exception:
    tk.Label(root, text="RB", font=("Segoe UI", 36, "bold"), fg=BRAND_BLUE, bg=BG).pack(pady=(52,0))
tk.Label(root, text="Row-Bot", font=("Segoe UI", 28, "bold"), fg=BRAND_BLUE, bg=BG).pack(pady=(0,10))
lbl = tk.Label(root, text="Loading.", font=("Segoe UI", 12), fg="#aaaaaa", bg=BG); lbl.pack()
try:
    root.update_idletasks()
    root.update()
    if READY_MARKER:
        with open(READY_MARKER, "w", encoding="utf-8") as fh:
            fh.write(f"ready pid={os.getpid()} time={time.time()}\n")
except Exception:
    pass
_start, _d = time.monotonic(), [0]
def _check():
    _d[0] = (_d[0] % 3) + 1; lbl.configure(text="Loading" + "." * _d[0])
    if time.monotonic() - _start > TIMEOUT or port_ready(): root.destroy(); return
    root.after(500, _check)
root.after(500, _check); root.mainloop()
"""

# Console fallback — used when tkinter is unavailable.
_SPLASH_CONSOLE = r"""
import sys, socket, time, os
PORT, TIMEOUT = int(sys.argv[1]), float(sys.argv[2])
if os.name == 'nt':
    os.system('title Row-Bot')
def port_ready():
    try:
        s = socket.socket(); s.settimeout(0.3)
        s.connect(("127.0.0.1", PORT)); s.close(); return True
    except OSError: return False
print("\n  Row-Bot — Starting...\n")
_start, _d = time.monotonic(), 0
while time.monotonic() - _start < TIMEOUT:
    if port_ready(): break
    _d = (_d % 3) + 1
    print(f"\r  Loading{'.' * _d}{'   '}", end="", flush=True)
    time.sleep(0.5)
print("\r  Ready!       ")
time.sleep(0.6)
"""


def _show_splash(port: int = _PORT, timeout: float = 60.0) -> subprocess.Popen | None:
    """Launch a splash screen subprocess.  Tries tkinter first; falls back
    to a simple console window if tkinter is unavailable."""
    _launch_event(
        "splash_start",
        port=port,
        tcl=os.environ.get("TCL_LIBRARY", ""),
        tk=os.environ.get("TK_LIBRARY", ""),
        python=sys.executable,
    )
    log_dir = _row_bot_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    splash_log = log_dir / "splash.log"
    ready_marker = _helper_artifact_path("splash", "ready")

    try:
        # --- Attempt 1: tkinter GUI splash ---
        proc = _start_launcher_helper(
            name="splash_tk",
            source=_SPLASH_TK,
            args=[
                str(port),
                str(timeout),
                str(_APP_GLYPH_PATH),
                str(_APP_ICON_PATH),
                APP_BRAND_ACCENT,
            ],
            ready_marker=ready_marker,
            log_path=splash_log,
            ready_timeout=3.0,
        )
        if proc is not None and proc.poll() is None:
            _launch_event("splash_tk_running", port=port, pid=proc.pid)
            return proc  # tkinter splash is running
        _launch_event(
            "splash_tk_exited", port=port, status=_launcher_helper_tail(splash_log)
        )
        if (
            sys.platform == "win32"
            and os.environ.get("ROW_BOT_SPLASH_CONSOLE_FALLBACK") != "1"
        ):
            logger.info("Tk splash exited; skipping Windows console splash fallback")
            return None
        # Tkinter often unavailable on macOS — silently fall back
        logger.debug("Tkinter splash exited, using console fallback")

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


# ── Native window (pywebview subprocess) ─────────────────────────────────────

_WINDOW_SCRIPT = r'''
import sys
import time
import os

# macOS: ensure the subprocess registers as a full GUI app so it can
# receive mouse/keyboard events.  Without this, a window re-opened from
# pystray renders but doesn't respond to input.
if sys.platform == "darwin":
    try:
        from AppKit import NSApplication, NSApp
        NSApplication.sharedApplication()
        NSApp.setActivationPolicy_(0)           # Regular (Dock) app
        NSApp.activateIgnoringOtherApps_(True)  # Bring to front
    except Exception:
        pass

import webview
import webbrowser
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from row_bot.buddy.config import get_buddy_config, save_buddy_config
from row_bot.buddy.overlay import (
    OVERLAY_HEIGHT,
    OVERLAY_WIDTH,
    BuddyPlacement,
    ForegroundAppTracker,
    apply_placement_state,
    enable_windows_per_monitor_dpi,
    finite_coordinate,
    native_overlay_transparency,
    placement_state_from_config,
    placement_state_for_app_startup,
    platform_foreground_backend,
    position_for_drop,
    screen_areas_from_native,
    should_defer_native_show,
)

_NAMED_WINDOWS = {}
_BUDDY_WINDOW_READY = False
_FOREGROUND_TRACKER = ForegroundAppTracker(platform_foreground_backend())

def _save_buddy_state(state, *, x=None, y=None):
    config = apply_placement_state(get_buddy_config(), state)
    overlay = dict(config.get("overlay") or {})
    if x is not None:
        overlay["x"] = int(x)
    if y is not None:
        overlay["y"] = int(y)
    config["overlay"] = overlay
    return save_buddy_config(config)

def _reset_buddy_placement_for_startup():
    config = get_buddy_config()
    current = placement_state_from_config(config)
    startup = placement_state_for_app_startup(config)
    if startup == current:
        return False
    _save_buddy_state(startup)
    _buddy_window_log("startup reset Buddy to the main dock")
    return True

def _screen_areas():
    try:
        return screen_areas_from_native(list(getattr(webview, "screens", []) or []))
    except Exception:
        return []

def _track_foreground_apps():
    while True:
        try:
            _FOREGROUND_TRACKER.observe()
        except Exception:
            pass
        time.sleep(0.25)

def _install_windows_app_icon():
    if sys.platform != "win32" or not (_ICON_PATH and os.path.isfile(_ICON_PATH)):
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ai.row-bot.desktop")
    except Exception:
        pass
    try:
        import webview.platforms.winforms as winforms
        from System.Drawing import Icon

        original_init = winforms.BrowserView.BrowserForm.__init__
        if getattr(original_init, "__row_bot_icon_patch__", False):
            return True

        def _row_bot_browser_form_init(self, window, cache_dir):
            original_init(self, window, cache_dir)
            try:
                self.Icon = Icon(_ICON_PATH)
            except Exception:
                pass

        _row_bot_browser_form_init.__row_bot_icon_patch__ = True
        winforms.BrowserView.BrowserForm.__init__ = _row_bot_browser_form_init
        return True
    except Exception as exc:
        try:
            print(f"Window icon patch failed: {exc}", file=sys.stderr, flush=True)
        except Exception:
            pass
        return False

def _port_from_url(url):
    try:
        parsed = urlparse(url)
        return int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception:
        return 8080

def _buddy_overlay_url(port, cache_bust=False):
    url = f"http://127.0.0.1:{int(port)}/buddy-overlay"
    if cache_bust:
        return f"{url}?buddy_refresh={int(time.time() * 1000)}"
    return url

def _buddy_window_log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [buddy.window] {message}"
    try:
        data_dir = os.environ.get("ROW_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".row-bot")
        log_dir = os.path.join(data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "row_bot_window.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    try:
        print(f"Buddy overlay lifecycle: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass

class _JsApi:
    """Expose Python helpers to JavaScript via window.pywebview.api."""
    def tear_off_buddy(self, screen_x=0, screen_y=0, port=None):
        try:
            buddy_port = int(port or _APP_PORT)
            drop_x = finite_coordinate(screen_x)
            drop_y = finite_coordinate(screen_y)
            x, y = position_for_drop(drop_x, drop_y, _screen_areas())
        except Exception:
            return False
        state = placement_state_from_config(get_buddy_config()).tear_off()
        _save_buddy_state(state, x=x, y=y)
        _buddy_window_log(f"tear-off drop={drop_x:.0f},{drop_y:.0f} position={x},{y}")
        opened = self.open_buddy_window(buddy_port, OVERLAY_WIDTH, OVERLAY_HEIGHT, x, y)
        if not opened:
            _save_buddy_state(state.dock())
        return opened

    def dock_buddy(self):
        state = placement_state_from_config(get_buddy_config()).dock()
        _save_buddy_state(state)
        _buddy_window_log("docked")
        return self.close_buddy_window(False)

    def buddy_placement(self):
        state = placement_state_from_config(get_buddy_config())
        return {
            "placement": state.placement.value,
            "visible": state.visible,
            "collapsed": state.collapsed,
        }

    def set_buddy_collapsed(self, collapsed=False):
        state = placement_state_from_config(get_buddy_config())
        if state.placement is not BuddyPlacement.DESKTOP:
            return False
        _save_buddy_state(state.collapse() if bool(collapsed) else state.expand())
        return True

    def show_main_window(self):
        window = _NAMED_WINDOWS.get("main")
        if window is None:
            return False
        try:
            try:
                window.restore()
            except Exception:
                pass
            window.show()
            return True
        except Exception:
            return False

    def get_foreground_target(self):
        target = _FOREGROUND_TRACKER.last_external
        return {
            "available": target is not None,
            "app_name": _FOREGROUND_TRACKER.app_name,
        }

    def restore_foreground_target(self):
        return _FOREGROUND_TRACKER.restore_once()

    def open_url(self, url):
        if isinstance(url, str) and url.lower().startswith(("https://", "http://")):
            webbrowser.open(url)

    def _dialog_window(self):
        try:
            windows = list(getattr(webview, "windows", []) or [])
            return windows[0] if windows else None
        except Exception:
            return None

    def _dialog_directory(self, initial_dir):
        path = str(initial_dir or "")
        return path if path and os.path.isdir(path) else ""

    def _dialog_file_types(self, file_types):
        filters = []
        if isinstance(file_types, (list, tuple)):
            for item in file_types:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    label = str(item[0] or "Files")
                    pattern = str(item[1] or "*.*")
                    filters.append(label if "(" in label else f"{label} ({pattern})")
                elif isinstance(item, str) and item.strip():
                    filters.append(item.strip())
        return tuple(filters)

    def choose_file(self, title="Select file", initial_dir="", file_types=None):
        window = self._dialog_window()
        if window is None:
            return None
        try:
            result = window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=self._dialog_directory(initial_dir),
                allow_multiple=False,
                file_types=self._dialog_file_types(file_types),
            )
        except Exception:
            return None
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return result

    def choose_folder(self, title="Select folder", initial_dir="", file_types=None):
        window = self._dialog_window()
        if window is None:
            return None
        try:
            result = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=self._dialog_directory(initial_dir),
                allow_multiple=False,
            )
        except Exception:
            return None
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return result

    def open_window(self, name, url, title=None, width=1600, height=900):
        if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
            return False

        try:
            width = int(width or 1600)
            height = int(height or 900)
        except Exception:
            width, height = 1600, 900

        key = name if isinstance(name, str) else ""
        if key:
            existing = _NAMED_WINDOWS.get(key)
            if existing is not None:
                try:
                    existing.load_url(url)
                    try:
                        existing.restore()
                    except Exception:
                        pass
                    try:
                        existing.show()
                    except Exception:
                        pass
                    return True
                except Exception:
                    _NAMED_WINDOWS.pop(key, None)

        window = webview.create_window(
            title or "Row-Bot",
            url,
            width=width,
            height=height,
            js_api=_JS_API,
        )
        if key:
            _NAMED_WINDOWS[key] = window

            def _forget(*_args):
                _NAMED_WINDOWS.pop(key, None)

            try:
                window.events.closed += _forget
            except Exception:
                pass
        return True

    def close_window(self, name):
        key = name if isinstance(name, str) else ""
        if not key:
            return False
        window = _NAMED_WINDOWS.get(key)
        if window is None:
            return False
        try:
            window.destroy()
            _NAMED_WINDOWS.pop(key, None)
            return True
        except Exception:
            return False

    def open_buddy_window(self, port=None, width=OVERLAY_WIDTH, height=OVERLAY_HEIGHT, x=None, y=None):
        global _BUDDY_WINDOW_READY
        try:
            buddy_port = int(port or _APP_PORT)
            width = int(width or OVERLAY_WIDTH)
            height = int(height or OVERLAY_HEIGHT)
        except Exception:
            return False

        config = get_buddy_config()
        placement = placement_state_from_config(config)
        if placement.placement is not BuddyPlacement.DESKTOP:
            return False
        overlay_config = dict(config.get("overlay") or {})
        requested_x = overlay_config.get("x") if x is None else x
        requested_y = overlay_config.get("y") if y is None else y
        if requested_x is not None and requested_y is not None:
            x, y = position_for_drop(
                finite_coordinate(requested_x) + width / 2,
                finite_coordinate(requested_y) + min(height / 3, 76),
                _screen_areas(),
                width=width,
                height=height,
            )
            _save_buddy_state(placement, x=x, y=y)
        else:
            x = y = None
        key = "buddy"
        url = _buddy_overlay_url(buddy_port)
        existing = _NAMED_WINDOWS.get(key)
        if existing is not None:
            try:
                if x is not None and y is not None:
                    existing.move(int(x), int(y))
                try:
                    existing.restore()
                except Exception:
                    pass
                if placement.visible and _BUDDY_WINDOW_READY:
                    existing.show()
                return True
            except Exception:
                _NAMED_WINDOWS.pop(key, None)

        kwargs = {
            "title": "Buddy",
            "url": url,
            "width": width,
            "height": height,
            "x": int(x) if x is not None else None,
            "y": int(y) if y is not None else None,
            "js_api": _JS_API,
            "resizable": False,
            "frameless": True,
            "shadow": False,
            "focus": True,
            "on_top": True,
            "easy_drag": False,
            "hidden": True,
            "background_color": "#0B1119",
            "transparent": native_overlay_transparency(sys.platform),
        }
        window = None
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("background_color", None)
        minimal_kwargs = dict(fallback_kwargs)
        for optional_key in ("transparent", "shadow", "on_top", "hidden"):
            minimal_kwargs.pop(optional_key, None)
        plain_kwargs = {
            "title": "Buddy",
            "url": url,
            "width": width,
            "height": height,
            "js_api": _JS_API,
        }
        for candidate in (kwargs, fallback_kwargs, minimal_kwargs, plain_kwargs):
            try:
                window = webview.create_window(**candidate)
                break
            except Exception as exc:
                try:
                    print(f"Buddy overlay create_window failed with keys {sorted(candidate.keys())}: {exc}", file=sys.stderr, flush=True)
                except Exception:
                    pass
                continue
        if window is None:
            try:
                window = webview.create_window("Buddy", url, width=width, height=height, js_api=_JS_API)
            except Exception as exc:
                try:
                    print(f"Buddy overlay plain create_window failed: {exc}", file=sys.stderr, flush=True)
                except Exception:
                    pass
        if window is None:
            return False
        _BUDDY_WINDOW_READY = False
        _NAMED_WINDOWS[key] = window

        def _forget_buddy(*_args):
            global _BUDDY_WINDOW_READY
            _NAMED_WINDOWS.pop(key, None)
            _BUDDY_WINDOW_READY = False

        def _persist_move(moved_x, moved_y):
            try:
                state = placement_state_from_config(get_buddy_config())
                _save_buddy_state(state, x=int(moved_x), y=int(moved_y))
            except Exception:
                pass

        try:
            window.events.closed += _forget_buddy
            window.events.moved += _persist_move
        except Exception:
            pass

        def _ready_timeout_show():
            time.sleep(2.0)
            if _NAMED_WINDOWS.get(key) is not window or _BUDDY_WINDOW_READY:
                return
            state = placement_state_from_config(get_buddy_config())
            if state.placement is not BuddyPlacement.DESKTOP or not state.visible:
                return
            try:
                window.show()
                _buddy_window_log("page-ready handshake timed out; forced native show")
            except Exception as exc:
                _buddy_window_log(f"page-ready timeout show failed: {exc}")

        threading.Thread(
            target=_ready_timeout_show,
            daemon=True,
            name="buddy-ready-timeout",
        ).start()
        return True

    def mark_buddy_window_ready(self):
        global _BUDDY_WINDOW_READY
        _BUDDY_WINDOW_READY = True
        window = _NAMED_WINDOWS.get("buddy")
        if window is None:
            return False
        state = placement_state_from_config(get_buddy_config())
        if state.placement is not BuddyPlacement.DESKTOP or not state.visible:
            return True
        try:
            try:
                window.restore()
            except Exception:
                pass
            window.show()
            _buddy_window_log("page ready; native window shown")
            return True
        except Exception as exc:
            _buddy_window_log(f"page ready show failed: {exc}")
            return False

    def show_buddy_window(self, manual=True, port=None, width=OVERLAY_WIDTH, height=OVERLAY_HEIGHT):
        state = placement_state_from_config(get_buddy_config())
        if state.placement is not BuddyPlacement.DESKTOP:
            return False
        if bool(manual):
            _save_buddy_state(state.show())
        window = _NAMED_WINDOWS.get("buddy")
        if window is None:
            return self.open_buddy_window(port, width, height)
        if should_defer_native_show(ready=_BUDDY_WINDOW_READY, manual=bool(manual)):
            return True
        try:
            try:
                window.restore()
            except Exception:
                pass
            window.show()
            if bool(manual) and not _BUDDY_WINDOW_READY:
                _buddy_window_log("manual recovery forced native show before page-ready handshake")
            return True
        except Exception as exc:
            _buddy_window_log(f"native show failed: {exc}")
            return False

    def hide_buddy_window(self, manual=True):
        state = placement_state_from_config(get_buddy_config())
        if state.placement is not BuddyPlacement.DESKTOP:
            return False
        if bool(manual):
            _save_buddy_state(state.hide())
        window = _NAMED_WINDOWS.get("buddy")
        if window is None:
            return False
        try:
            window.hide()
            return True
        except Exception:
            return False

    def close_buddy_window(self, manual=True):
        global _BUDDY_WINDOW_READY
        window = _NAMED_WINDOWS.get("buddy")
        if window is None:
            return True
        try:
            window.destroy()
            _NAMED_WINDOWS.pop("buddy", None)
            _BUDDY_WINDOW_READY = False
            return True
        except Exception:
            return False

    def get_clipboard(self):
        import subprocess as _sp, sys as _sys
        try:
            if _sys.platform == "darwin":
                return _sp.check_output(["pbpaste"], timeout=2).decode("utf-8", errors="replace")
            elif _sys.platform == "win32":
                r = _sp.check_output(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                    timeout=2,
                )
                return r.decode("utf-8", errors="replace").rstrip("\r\n")
            else:
                for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
                    try:
                        return _sp.check_output(cmd, timeout=2).decode("utf-8", errors="replace")
                    except Exception:
                        pass
                return None
        except Exception:
            return None

def _on_loaded(window):
    try:
        window.evaluate_js("""
            (function() {
                var retries = 0;
                function check() {
                    if (retries >= 20) return;
                    var body = document.body;
                    if (!body || body.innerHTML.trim() === "") {
                        retries++;
                        setTimeout(function() { location.reload(); }, 1500);
                    }
                }
                setTimeout(check, 2000);
            })();
        """)
    except Exception:
        pass

_JS_API = _JsApi()

def _on_main_window_closing():
    state = placement_state_from_config(get_buddy_config())
    if state.placement is not BuddyPlacement.DESKTOP:
        return True
    window = _NAMED_WINDOWS.get("main")
    try:
        if window is not None:
            window.hide()
        _buddy_window_log("main-window close cancelled; hidden while Buddy is desktop")
        return False
    except Exception as exc:
        _buddy_window_log(f"main-window hide-on-close failed: {exc}")
        return True

def _install_main_window_buddy_events(window):
    try:
        window.events.closing += _on_main_window_closing
        _buddy_window_log("main-window native event sync installed")
        return True
    except Exception as exc:
        _buddy_window_log(f"main-window native event sync failed: {exc}")
        return False

def _start_control_server(control_port):
    try:
        control_port = int(control_port or 0)
    except Exception:
        control_port = 0
    if control_port <= 0:
        return

    class _ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, ok):
            self.send_response(200 if ok else 409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(("{\"ok\":true}" if ok else "{\"ok\":false}").encode("utf-8"))

        def do_GET(self):
            path = str(getattr(self, "path", ""))
            parsed = urlparse(path)
            qs = parse_qs(parsed.query or "")
            manual = str((qs.get("manual") or ["1"])[0]).lower() not in {"0", "false", "no"}
            if parsed.path.startswith("/buddy/show"):
                self._send(_JS_API.show_buddy_window(manual, _APP_PORT, OVERLAY_WIDTH, OVERLAY_HEIGHT))
            elif parsed.path.startswith("/buddy/hide"):
                self._send(_JS_API.hide_buddy_window(manual))
            elif parsed.path.startswith("/buddy/close"):
                self._send(_JS_API.close_buddy_window(manual))
            elif parsed.path.startswith("/main/show"):
                self._send(_JS_API.show_main_window())
            else:
                self.send_response(404)
                self.end_headers()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", control_port), _ControlHandler)
    except Exception:
        return
    threading.Thread(target=server.serve_forever, daemon=True, name="buddy-window-control").start()

url, title = sys.argv[1], sys.argv[2]
_APP_PORT = _port_from_url(url)
w, h = int(sys.argv[3]), int(sys.argv[4])
_ICON_PATH = sys.argv[5] if len(sys.argv) > 5 else ""
_CONTROL_PORT = int(sys.argv[6]) if len(sys.argv) > 6 else 0
enable_windows_per_monitor_dpi(sys.platform)
_install_windows_app_icon()
_reset_buddy_placement_for_startup()
_start_control_server(_CONTROL_PORT)
threading.Thread(target=_track_foreground_apps, daemon=True, name="buddy-foreground-tracker").start()
main_window = webview.create_window(title, url, width=w, height=h, js_api=_JS_API)
_NAMED_WINDOWS["main"] = main_window
_install_main_window_buddy_events(main_window)
_DATA_DIR = os.environ.get("ROW_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".row-bot")
_WEBVIEW_STORAGE_PATH = os.environ.get("ROW_BOT_WEBVIEW_STORAGE_PATH") or os.path.join(
    _DATA_DIR,
    "browser_profile",
    "pywebview",
)
try:
    os.makedirs(_WEBVIEW_STORAGE_PATH, exist_ok=True)
except Exception:
    pass
webview.start(
    func=_on_loaded,
    private_mode=False,
    storage_path=_WEBVIEW_STORAGE_PATH,
    icon=_ICON_PATH if _ICON_PATH and os.path.isfile(_ICON_PATH) else None,
)
'''


def _load_window_mode() -> str:
    """Read the preferred window mode from app_config.json.

    Returns ``'ask'`` (default / first launch), ``'native'``, or ``'browser'``.
    """
    config_path = _row_bot_data_dir() / "app_config.json"
    try:
        import json

        cfg = json.loads(config_path.read_text())
        mode = cfg.get("window_mode", "ask")
        if mode in ("ask", "native", "browser"):
            return mode
    except Exception:
        pass
    return "ask"


# Tkinter chooser dialog — shown when window_mode is "ask".
_CHOOSER_TK = r"""
import os, sys

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

GLYPH_PATH = sys.argv[1] if len(sys.argv) > 1 else ""
ICON_PATH = sys.argv[2] if len(sys.argv) > 2 else ""
BRAND_BLUE = sys.argv[3] if len(sys.argv) > 3 else "#4F78A4"
RESULT_PATH = sys.argv[4] if len(sys.argv) > 4 else ""
READY_MARKER = sys.argv[5] if len(sys.argv) > 5 else ""
BG, GREY = "#1e1e1e", "#aaaaaa"
choice = ["native"]

def pick(mode):
    choice[0] = mode
    if RESULT_PATH:
        try:
            with open(RESULT_PATH, "w", encoding="utf-8") as fh:
                fh.write(mode + "\n")
        except Exception:
            pass
    root.destroy()

root = tk.Tk()
root.title("Row-Bot")
root.configure(bg=BG)
root.resizable(False, False)
try:
    if ICON_PATH and os.path.isfile(ICON_PATH):
        root.iconbitmap(ICON_PATH)
except Exception:
    pass
sx, sy = root.winfo_screenwidth(), root.winfo_screenheight()
root.geometry(f"420x280+{(sx-420)//2}+{(sy-280)//2}")
root.attributes("-topmost", True)

try:
    logo = tk.PhotoImage(file=GLYPH_PATH)
    max_side = max(int(logo.width()), int(logo.height()), 1)
    factor = max(1, round(max_side / 88))
    if factor > 1:
        logo = logo.subsample(factor, factor)
    root._row_bot_logo = logo
    tk.Label(root, image=logo, bg=BG).pack(pady=(20,0))
except Exception:
    tk.Label(root, text="RB", font=("Segoe UI", 30, "bold"), fg=BRAND_BLUE, bg=BG).pack(pady=(24,0))
tk.Label(root, text="How would you like to open Row-Bot?",
         font=("Segoe UI", 14), fg="#ffffff", bg=BG).pack(pady=(10,16))

btn_frame = tk.Frame(root, bg=BG)
btn_frame.pack()

btn_cfg = dict(font=("Segoe UI", 12), width=18, cursor="hand2",
               relief="flat", bd=0, pady=8)
tk.Button(btn_frame, text="\U0001f5d4  Native Window", bg="#2a2a3e", fg="#ffffff",
          activebackground="#3a3a5e", activeforeground="#ffffff",
          command=lambda: pick("native"), **btn_cfg).pack(side="left", padx=8)
tk.Button(btn_frame, text="\U0001f310  System Browser", bg="#2a2a3e", fg="#ffffff",
          activebackground="#3a3a5e", activeforeground="#ffffff",
          command=lambda: pick("browser"), **btn_cfg).pack(side="left", padx=8)

tk.Label(root, text="To set a default, go to Settings \u2192 System.",
         font=("Segoe UI", 10), fg=GREY, bg=BG).pack(pady=(16,0))

root.protocol("WM_DELETE_WINDOW", lambda: pick("native"))
try:
    root.update_idletasks()
    root.update()
    if READY_MARKER:
        with open(READY_MARKER, "w", encoding="utf-8") as fh:
            fh.write(f"ready pid={os.getpid()}\n")
except Exception:
    pass
root.mainloop()
print(choice[0])
"""

# Console fallback chooser — used when tkinter is unavailable.
_CHOOSER_CONSOLE = r"""
import sys, os
RESULT_PATH = sys.argv[1] if len(sys.argv) > 1 else ""
if os.name == 'nt':
    os.system('title Row-Bot')
print()
print("  Row-Bot")
print("  " + "-" * 36)
print()
print("  How would you like to open Row-Bot?")
print()
print("  1) Native Window")
print("  2) System Browser")
print()
print("  To set a default, go to")
print("  Settings \u2192 System.")
print()
while True:
    try:
        c = input("  Enter 1 or 2: ").strip()
    except (EOFError, KeyboardInterrupt):
        c = "1"
        break
    if c in ("1", "2"):
        break
    print("  Please enter 1 or 2.")
mode = "native" if c == "1" else "browser"
if RESULT_PATH:
    try:
        with open(RESULT_PATH, "w", encoding="utf-8") as fh:
            fh.write(mode + "\n")
    except Exception:
        pass
print(mode)
"""


def _ask_window_mode() -> str:
    """Show the first-run window chooser, defaulting quickly if it fails."""
    log_dir = _row_bot_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    chooser_log = log_dir / "window-mode-chooser.log"
    ready_marker = _helper_artifact_path("window-mode-chooser", "ready")
    result_path = _helper_artifact_path("window-mode-chooser", "result")
    _delete_if_exists(result_path)

    proc = _start_launcher_helper(
        name="window_mode_chooser_tk",
        source=_CHOOSER_TK,
        args=[
            str(_APP_GLYPH_PATH),
            str(_APP_ICON_PATH),
            APP_BRAND_ACCENT,
            str(result_path),
        ],
        ready_marker=ready_marker,
        log_path=chooser_log,
        ready_timeout=3.0,
    )
    if proc is not None:
        exit_code = _wait_for_launcher_helper(
            proc,
            name="window_mode_chooser_tk",
            timeout=120.0,
            log_path=chooser_log,
        )
        if exit_code == 0 and result_path.exists():
            result = (
                result_path.read_text(encoding="utf-8", errors="replace")
                .strip()
                .splitlines()[-1]
            )
            if result in ("native", "browser"):
                logger.info("User chose window mode: %s (GUI)", result)
                return result
        logger.warning(
            "Window mode GUI chooser exited without a valid result (code %s, log=%s): %s",
            exit_code,
            chooser_log,
            _launcher_helper_tail(chooser_log),
        )
    else:
        logger.warning(
            "Window mode GUI chooser unavailable; defaulting to native unless console fallback is explicitly enabled"
        )

    if (
        sys.platform == "win32"
        and os.environ.get("ROW_BOT_WINDOW_MODE_CONSOLE_FALLBACK") != "1"
    ):
        logger.info(
            "Skipping Windows console window mode fallback; defaulting to native"
        )
        return "native"

    try:
        _delete_if_exists(result_path)
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_CONSOLE
            console_proc = subprocess.Popen(
                [sys.executable, "-c", _CHOOSER_CONSOLE, str(result_path)],
                creationflags=flags,
            )
            _wait_for_launcher_helper(
                console_proc,
                name="window_mode_chooser_console",
                timeout=120.0,
                log_path=chooser_log,
            )
            result = (
                result_path.read_text(encoding="utf-8", errors="replace")
                .strip()
                .splitlines()[-1]
                if result_path.exists()
                else ""
            )
        else:
            completed = subprocess.run(
                [sys.executable, "-c", _CHOOSER_CONSOLE, str(result_path)],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=flags,
            )
            if result_path.exists():
                result = (
                    result_path.read_text(encoding="utf-8", errors="replace")
                    .strip()
                    .splitlines()[-1]
                )
            else:
                result = (
                    completed.stdout.strip().splitlines()[-1]
                    if completed.stdout.strip()
                    else ""
                )
        if result in ("native", "browser"):
            logger.info("User chose window mode: %s (console)", result)
            return result
    except Exception as exc:
        logger.warning(
            "Window mode chooser failed: %s; defaulting to native (log=%s): %s",
            exc,
            chooser_log,
            _launcher_helper_tail(chooser_log),
        )
    return "native"


def _open_in_browser(port: int = _PORT) -> None:
    """Open the Row-Bot UI in the default system browser."""
    webbrowser.open(_url_for_port(port))
    logger.info("Opened %s in system browser on port %s", APP_DISPLAY_NAME, port)


def _open_window(
    port: int = _PORT, control_port: int | None = None
) -> subprocess.Popen | None:
    """Open a pywebview native window pointing at the running server.

    Returns the subprocess handle, or None on failure.
    The window is a standalone process — closing it does NOT
    affect the server.
    """
    if not _has_display_server():
        logger.warning(
            "No display server detected; opening browser instead of native window"
        )
        webbrowser.open(_url_for_port(port))
        return None
    try:
        args = [
            sys.executable,
            "-c",
            _WINDOW_SCRIPT,
            _url_for_port(port),
            APP_DISPLAY_NAME,
            "1280",
            "900",
            str(_APP_ICON_PATH),
        ]
        if control_port:
            args.append(str(int(control_port)))
        proc = subprocess.Popen(
            args,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(0.5)
        if proc.poll() is not None:
            logger.warning(
                "Native window exited during startup; falling back to browser"
            )
            webbrowser.open(_url_for_port(port))
            return None
        logger.info("Native window opened (PID %s, port %s)", proc.pid, port)
        return proc
    except Exception as exc:
        logger.warning(
            "Could not open native window: %s — falling back to browser", exc
        )
        webbrowser.open(_url_for_port(port))
        return None


def _wait_for_server(
    port: int = _PORT,
    timeout: float | None = None,
    server: _RowBotProcess | None = None,
) -> bool:
    """Block until the NiceGUI server is reachable, or *timeout* expires."""
    deadline = time.monotonic() + (
        timeout if timeout is not None else _startup_timeout()
    )
    while time.monotonic() < deadline:
        if server is not None and not server.is_alive:
            return False
        launch_secret = (
            server._launch_environment.get(LAUNCH_SECRET_ENV)
            if server is not None
            else None
        )
        if _is_row_bot_server(port, launch_secret=launch_secret):
            return True
        time.sleep(0.3)
    return False


# ── Tray application ────────────────────────────────────────────────────────


class RowBotTray:
    """System-tray icon that manages the NiceGUI server and native window."""

    def __init__(
        self,
        *,
        preferred_port: int = _PORT,
        host: str | None = None,
        preferred_mode: str | None = None,
        no_splash: bool = False,
        no_ollama: bool = False,
    ) -> None:
        self._preferred_port = preferred_port
        self._port = preferred_port
        self._explicit_host = host
        self._host = _resolve_launch_host(host)
        self._preferred_mode = preferred_mode
        self._no_splash = no_splash
        self._no_ollama = no_ollama
        self._server = _RowBotProcess(self._port, host=self._host)
        _set_process_launch_environment(
            self._server, {"ROW_BOT_DEPLOYMENT_MODE": "desktop"}
        )
        self._owns_server = False  # True if *we* started it
        self._restart_lock = threading.Lock()
        self._launcher_control: LauncherControlServer | None = None
        self._window_proc: subprocess.Popen | None = None
        self._window_control_port: int | None = None
        self._stop_event = threading.Event()
        self._quitting = False

        self._icon = _create_tray_backend(_build_tray_menu_entries(self))

    # ── Helpers ──────────────────────────────────────────────────────────

    def _is_window_alive(self) -> bool:
        return self._window_proc is not None and self._window_proc.poll() is None

    def _ensure_window_control_port(self) -> int:
        if self._window_control_port is None:
            self._window_control_port = _find_free_port(
                self._port + 10000, max_tries=50
            )
        return self._window_control_port

    def _ensure_launcher_control(self) -> None:
        if self._launcher_control is not None:
            return
        control = LauncherControlServer(self._restart_child_from_control)
        control.start()
        self._launcher_control = control
        _set_process_launch_environment(
            self._server,
            control.child_environment(),
        )
        atexit.register(control.stop)

    def _restart_child_from_control(self) -> None:
        """Restart only the child owned by this launcher."""
        if not self._owns_server:
            return
        if not self._restart_lock.acquire(blocking=False):
            return
        try:
            self._server.stop()
            deadline = time.monotonic() + 10.0
            while _is_port_in_use(self._port) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _is_port_in_use(self._port):
                logger.error("Launcher-controlled restart timed out waiting for port")
                return
            self._host = _resolve_launch_host(self._explicit_host)
            self._server.start(self._port, self._host)
            if not _wait_for_server(self._port, server=self._server):
                logger.error("Launcher-controlled child restart failed readiness")
        except Exception:
            logger.exception("Launcher-controlled child restart failed")
        finally:
            self._restart_lock.release()

    def _send_window_command(self, action: str, timeout: float = 1.5) -> bool:
        port = self._window_control_port
        if not port:
            return False
        path = "/main/show" if action == "main-show" else f"/buddy/{action}"
        url = f"http://127.0.0.1:{int(port)}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read(64).decode("utf-8", errors="replace").lower()
                return getattr(response, "status", 200) == 200 and "true" in body
        except (OSError, urllib.error.URLError, TimeoutError):
            return False

    def _ensure_native_window_for_buddy(self) -> bool:
        if self._is_window_alive():
            return True
        if self._owns_server and not self._server.is_alive:
            self._server.stop()
            for _ in range(10):
                if not _is_port_in_use(self._port):
                    break
                time.sleep(0.5)
            self._server.start(self._port)
            if not _wait_for_server(self._port, server=self._server):
                return False
        elif not _is_row_bot_server(self._port):
            return False
        self._window_proc = _open_window(self._port, self._ensure_window_control_port())
        if not self._is_window_alive():
            return False
        time.sleep(0.7)
        return True

    # ── Menu callbacks ───────────────────────────────────────────────────

    def _on_open(self, icon=None, item=None) -> None:  # noqa: ARG002
        """Open (or re-open) the native window."""
        if self._is_window_alive():
            if self._send_window_command("main-show"):
                logger.info("Restored existing hidden %s window", APP_DISPLAY_NAME)
                return
            # Window is running but may be behind other apps.  Try to bring
            # it to the foreground WITHOUT killing it — killing drops the
            # WebSocket connection and makes in-flight streams appear failed.
            _brought = False
            if sys.platform == "darwin":
                try:
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            'tell application "System Events" to set '
                            "frontmost of every process whose unix id is "
                            f"{self._window_proc.pid} to true",
                        ],
                        timeout=3,
                        capture_output=True,
                    )
                    _brought = True
                except Exception:
                    pass
            elif sys.platform == "win32":
                try:
                    import ctypes

                    hwnd = ctypes.windll.user32.FindWindowW(None, APP_DISPLAY_NAME)
                    if hwnd:
                        SW_RESTORE = 9
                        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                        _brought = True
                except Exception:
                    pass
            else:
                # Linux / other — try wmctrl
                try:
                    subprocess.run(
                        ["wmctrl", "-a", APP_DISPLAY_NAME],
                        timeout=3,
                        capture_output=True,
                    )
                    _brought = True
                except Exception:
                    pass

            if _brought:
                logger.info(
                    "Brought existing window to front (pid %s)", self._window_proc.pid
                )
                return  # keep existing WebSocket connection alive

            # Platform trick failed — kill and spawn fresh window.
            _RowBotProcess._terminate_process(
                self._window_proc,
                label=f"{APP_DISPLAY_NAME} window",
                timeout=3,
                kill_tree=True,
            )
            self._window_proc = None

        if self._owns_server and not self._server.is_alive:
            # Server crashed — restart it first
            logger.info("Server not running — restarting before opening window")
            self._server.stop()
            for _ in range(10):
                if not _is_port_in_use(self._port):
                    break
                time.sleep(0.5)
            self._server.start(self._port)
            _wait_for_server(self._port, server=self._server)

        if not self._owns_server and not _is_row_bot_server(self._port):
            # External server died — just open browser and hope
            _launch_event("server_ready_timeout", port=self._port, duration_ms=0.0)
            webbrowser.open(_url_for_port(self._port))
            return

        logger.info("Opening %s window", APP_DISPLAY_NAME)
        self._window_proc = _open_window(self._port, self._ensure_window_control_port())

    def _on_open_browser(self, icon=None, item=None) -> None:  # noqa: ARG002
        """Open the Row-Bot UI in the default system browser."""
        if _is_row_bot_server(self._port):
            _open_in_browser(self._port)
        elif self._owns_server:
            logger.info("Server not running — restarting before opening browser")
            self._server.stop()
            for _ in range(10):
                if not _is_port_in_use(self._port):
                    break
                time.sleep(0.5)
            self._server.start(self._port)
            if _wait_for_server(self._port, server=self._server):
                _open_in_browser(self._port)
                _launch_event("browser_opened", port=self._port, mode="browser")
            else:
                logger.warning("Server did not restart — cannot open browser")
        else:
            _open_in_browser(self._port)

    def _on_show_buddy(self, icon=None, item=None) -> None:  # noqa: ARG002
        """Show the desktop Buddy overlay from the system tray."""
        if self._ensure_native_window_for_buddy() and self._send_window_command("show"):
            logger.info("Buddy overlay shown from tray")
        else:
            logger.warning("Could not show Buddy overlay from tray")

    def _on_hide_buddy(self, icon=None, item=None) -> None:  # noqa: ARG002
        """Hide the desktop Buddy overlay from the system tray."""
        if self._send_window_command("hide"):
            logger.info("Buddy overlay hidden from tray")
        else:
            logger.info("Buddy overlay was not open")

    def _on_quit(self, icon=None, item=None) -> None:  # noqa: ARG002
        logger.info("Quit requested")
        if self._quitting:
            logger.info("Quit already in progress")
            return
        self._quitting = True
        self._stop_event.set()

        def _quit_watchdog() -> None:
            time.sleep(_QUIT_WATCHDOG_TIMEOUT)
            logger.warning("Quit watchdog forcing launcher exit after timeout")
            os._exit(0)

        def _quit_worker() -> None:
            try:
                if self._is_window_alive():
                    _RowBotProcess._terminate_process(
                        self._window_proc,
                        label=f"{APP_DISPLAY_NAME} window",
                        timeout=3,
                        kill_tree=True,
                    )
                    self._window_proc = None
                if self._owns_server:
                    self._server.stop()
                if self._launcher_control is not None:
                    self._launcher_control.stop()
                _clear_launcher_state()
            finally:
                try:
                    self._icon.stop()
                except Exception:
                    logger.debug("Could not stop tray icon during quit", exc_info=True)
                logger.info("Launcher quit complete")
                os._exit(0)

        threading.Thread(
            target=_quit_watchdog, daemon=True, name="quit-watchdog"
        ).start()
        threading.Thread(target=_quit_worker, daemon=False, name="quit-worker").start()
        try:
            self._icon.stop()
        except Exception:
            logger.debug("Could not stop tray icon immediately", exc_info=True)

    # ── Background poller ────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Periodically check if the server is alive and update icon."""
        _POLL_INTERVAL = 3.0  # seconds
        _crash_logged = False
        while not self._stop_event.is_set():
            if self._owns_server and self._server.is_alive:
                self._icon.icon = _get_icon("running")
                self._icon.title = f"{APP_DISPLAY_NAME} — running"
                _crash_logged = False
            elif not self._owns_server and _is_row_bot_server(self._port):
                self._icon.icon = _get_icon("running")
                self._icon.title = f"{APP_DISPLAY_NAME} — running"
            else:
                self._icon.icon = _get_icon("stopped")
                self._icon.title = f"{APP_DISPLAY_NAME} — stopped"
                # Log once when the server process dies unexpectedly
                if self._owns_server and not _crash_logged:
                    _crash_logged = True
                    rc = self._server._proc.returncode if self._server._proc else "?"
                    log_path = self._server._log_file or "?"
                    logger.error(
                        "%s server exited (code %s). Check %s for details.",
                        APP_DISPLAY_NAME,
                        rc,
                        log_path,
                    )
                    if self._server._log_file and self._server._log_file.exists():
                        try:
                            log_text = self._server._log_file.read_text(
                                encoding="utf-8", errors="replace"
                            )
                            tail = log_text.strip().splitlines()[-10:]
                            if tail:
                                logger.error("--- last lines of log ---")
                                for line in tail:
                                    logger.error("  %s", line)
                                logger.error("--- end of log ---")
                        except Exception:
                            pass
                        _log_startup_failure_hints(self._server._log_file)

            self._stop_event.wait(_POLL_INTERVAL)

    # ── Entry point ──────────────────────────────────────────────────────

    def _run_startup_sequence(self) -> None:
        """Start server/window work once the tray backend is ready."""
        splash_proc: subprocess.Popen | None = _claim_early_splash()
        ollama_started = time.perf_counter()
        # Best-effort local runtime convenience. Provider-only and custom
        # endpoint setups should not pay an Ollama startup penalty.
        _maybe_start_ollama(no_ollama=self._no_ollama)
        _launch_event(
            "ollama_gate_done",
            duration_ms=round((time.perf_counter() - ollama_started) * 1000.0, 1),
        )

        self._port, already_running = _select_app_port(self._preferred_port)
        self._server.port = self._port

        if already_running:
            logger.info("%s already running on port %s", APP_DISPLAY_NAME, self._port)
            _launch_event("server_already_running", port=self._port)
        else:
            server_started = time.perf_counter()
            self._ensure_launcher_control()
            self._server.start(self._port, self._host)
            _launch_event(
                "server_spawned",
                port=self._port,
                host=self._host,
                pid=self._server._proc.pid if self._server._proc else 0,
                duration_ms=round((time.perf_counter() - server_started) * 1000.0, 1),
            )
            self._owns_server = True
            # Register cleanup in case launcher crashes
            atexit.register(self._server.stop)

            # Show splash screen while the server starts up
            if splash_proc is None and not self._no_splash and _has_display_server():
                splash_started = time.perf_counter()
                splash_proc = _show_splash(self._port)
                _launch_event(
                    "splash_requested",
                    port=self._port,
                    duration_ms=round(
                        (time.perf_counter() - splash_started) * 1000.0, 1
                    ),
                )

        # Start the status-polling thread
        poller = threading.Thread(target=self._poll_loop, daemon=True, name="tray-poll")
        poller.start()

        # Wait for server to be ready, then open UI in the preferred mode
        wait_started = time.perf_counter()
        if _wait_for_server(self._port, server=self._server):
            _launch_event(
                "server_ready",
                port=self._port,
                duration_ms=round((time.perf_counter() - wait_started) * 1000.0, 1),
            )
            mode = self._preferred_mode or _load_window_mode()
            _stop_launcher_helper(splash_proc, name="splash_tk")
            if mode == "ask":
                mode = _ask_window_mode() if _has_display_server() else "browser"
            if mode == "browser":
                _open_in_browser(self._port)
                _write_launcher_state(
                    port=self._port,
                    mode="browser",
                    owns_server=self._owns_server,
                )
            else:
                self._window_proc = _open_window(
                    self._port, self._ensure_window_control_port()
                )
                if self._window_proc is None:
                    self._window_control_port = None
                _launch_event(
                    "native_window_requested",
                    port=self._port,
                    pid=self._window_proc.pid if self._window_proc else 0,
                    mode=mode,
                )
                _write_launcher_state(
                    port=self._port,
                    mode=mode,
                    owns_server=self._owns_server,
                    window_control_port=self._window_control_port,
                    window_pid=self._window_proc.pid if self._window_proc else None,
                )
        else:
            logger.warning("Server did not start in time — opening browser as fallback")
            webbrowser.open(_url_for_port(self._port))

        logger.info("%s tray startup complete", APP_DISPLAY_NAME)

    def run(self) -> None:
        """Start the tray icon, the NiceGUI server, and a native window."""
        _launch_event(
            "tray_run_start",
            port=self._preferred_port,
            mode=self._preferred_mode or "auto",
        )

        if sys.platform == "darwin":
            logger.info("Starting macOS tray run loop before launcher startup")

            def _setup(_icon) -> None:
                try:
                    self._run_startup_sequence()
                except Exception:
                    logger.exception("macOS tray startup failed")
                    try:
                        self._icon.stop()
                    except Exception:
                        logger.debug(
                            "Could not stop macOS tray after startup failure",
                            exc_info=True,
                        )

            self._icon.run(setup=_setup)
            return

        self._run_startup_sequence()
        self._icon.run()


# ── Direct / headless launcher mode ─────────────────────────────────────────


def _block_until_interrupted(
    server: _RowBotProcess | None,
    owns_server: bool,
    *,
    restart_in_progress: Callable[[], bool] | None = None,
) -> None:
    stop_requested = threading.Event()
    previous_handlers: dict[int, object] = {}

    def _request_stop(_signum, _frame) -> None:
        stop_requested.set()

    if threading.current_thread() is threading.main_thread():
        for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signal_value = getattr(signal, signal_name, None)
            if signal_value is None:
                continue
            try:
                previous_handlers[signal_value] = signal.getsignal(signal_value)
                signal.signal(signal_value, _request_stop)
            except (OSError, ValueError):
                continue
    try:
        while not stop_requested.wait(1.0):
            if owns_server and server is not None and not server.is_alive:
                if restart_in_progress is not None and restart_in_progress():
                    continue
                raise RuntimeError(f"{APP_DISPLAY_NAME} server exited unexpectedly")
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
    finally:
        for signal_value, previous in previous_handlers.items():
            try:
                signal.signal(signal_value, previous)
            except (OSError, ValueError):
                pass
        if owns_server and server is not None:
            server.stop()
            _clear_launcher_state()


def _run_direct(args: argparse.Namespace) -> None:
    """Run Row-Bot without a tray icon, for Linux/browser/server modes."""
    host_input = getattr(args, "_dynamic_host_input", args.host)
    resolved_host = _resolve_launch_host(host_input)
    _launch_event("direct_run_start", port=args.port, host=resolved_host, mode="direct")
    ollama_started = time.perf_counter()
    _maybe_start_ollama(no_ollama=args.no_ollama)
    _launch_event(
        "ollama_gate_done",
        duration_ms=round((time.perf_counter() - ollama_started) * 1000.0, 1),
    )

    preferred = parse_app_port(args.port, default=_PORT)
    port, already_running = _select_app_port(preferred)
    server = _RowBotProcess(port, host=resolved_host)
    _set_process_launch_environment(
        server,
        {"ROW_BOT_DEPLOYMENT_MODE": ("server" if bool(args.server) else "desktop")},
    )
    owns_server = False
    restart_lock = threading.Lock()
    launcher_control: LauncherControlServer | None = None
    splash_proc: subprocess.Popen | None = _claim_early_splash()

    def _restart_direct_child() -> None:
        if not owns_server or not restart_lock.acquire(blocking=False):
            return
        try:
            server.stop()
            deadline = time.monotonic() + 10.0
            while _is_port_in_use(port) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _is_port_in_use(port):
                logger.error("Launcher-controlled restart timed out waiting for port")
                return
            server.start(port, _resolve_launch_host(host_input))
            if not _wait_for_server(port, server=server):
                logger.error("Launcher-controlled child restart failed readiness")
        except Exception:
            logger.exception("Launcher-controlled child restart failed")
        finally:
            restart_lock.release()

    if already_running:
        logger.info("%s already running on port %s", APP_DISPLAY_NAME, port)
        _launch_event("server_already_running", port=port)
    else:
        server_started = time.perf_counter()
        launcher_control = LauncherControlServer(_restart_direct_child)
        launcher_control.start()
        _set_process_launch_environment(server, launcher_control.child_environment())
        server.start(port, resolved_host)
        _launch_event(
            "server_spawned",
            port=port,
            host=resolved_host,
            pid=server._proc.pid if server._proc else 0,
            duration_ms=round((time.perf_counter() - server_started) * 1000.0, 1),
        )
        owns_server = True
        atexit.register(server.stop)
        if (
            splash_proc is None
            and not args.no_splash
            and _has_display_server()
            and not args.server
        ):
            splash_started = time.perf_counter()
            splash_proc = _show_splash(port)
            _launch_event(
                "splash_requested",
                port=port,
                duration_ms=round((time.perf_counter() - splash_started) * 1000.0, 1),
            )

    wait_process = server if owns_server else None
    wait_started = time.perf_counter()
    if not _wait_for_server(port, server=wait_process):
        if launcher_control is not None:
            launcher_control.stop()
        _stop_launcher_helper(splash_proc, name="splash_tk")
        _launch_event(
            "server_ready_timeout",
            port=port,
            duration_ms=round((time.perf_counter() - wait_started) * 1000.0, 1),
        )
        reason = (
            "app process exited before readiness"
            if owns_server and not server.is_alive
            else "readiness probe timed out"
        )
        _log_startup_failure_context(server, port, reason)
        raise RuntimeError(
            f"{APP_DISPLAY_NAME} server did not become ready on port {port}"
        )
    _launch_event(
        "server_ready",
        port=port,
        duration_ms=round((time.perf_counter() - wait_started) * 1000.0, 1),
    )
    _stop_launcher_helper(splash_proc, name="splash_tk")

    mode_for_state = "server" if args.server or args.no_open else "browser"
    window_control_port: int | None = None
    window_proc: subprocess.Popen | None = None

    if not args.no_open:
        if args.native and _has_display_server():
            mode_for_state = "native"
            window_control_port = _find_free_port(port + 10000, max_tries=50)
            window_proc = _open_window(port, window_control_port)
            if window_proc is None:
                window_control_port = None
            _launch_event(
                "native_window_requested",
                port=port,
                pid=window_proc.pid if window_proc else 0,
                mode="native",
            )
        elif _has_display_server() or not args.server:
            mode_for_state = "browser"
            _open_in_browser(port)
            _launch_event("browser_opened", port=port, mode="browser")
        else:
            logger.info("%s is running at %s", APP_DISPLAY_NAME, _url_for_port(port))
    else:
        logger.info("%s is running at %s", APP_DISPLAY_NAME, _url_for_port(port))

    _write_launcher_state(
        port=port,
        mode=mode_for_state,
        owns_server=owns_server,
        window_control_port=window_control_port,
        window_pid=window_proc.pid if window_proc else None,
    )

    if owns_server:
        try:
            _block_until_interrupted(
                server,
                owns_server=True,
                restart_in_progress=restart_lock.locked,
            )
        finally:
            if launcher_control is not None:
                launcher_control.stop()


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup_family(path: Path, backup_dir: Path) -> list[tuple[Path, Path]]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if not src.exists():
            continue
        dst = backup_dir / src.name
        shutil.move(str(src), str(dst))
        moved.append((src, dst))
    return moved


def _reset_tasks_db() -> int:
    data_dir = _row_bot_data_dir()
    tasks_db = get_tasks_db_path()
    backup_dir = data_dir / "recovery" / f"tasks-db-reset-{_timestamp()}"
    moved = _backup_family(tasks_db, backup_dir)
    from row_bot.tasks import ensure_task_schema

    result = ensure_task_schema(repair=True, force=True)
    logger.info("Data dir: %s", data_dir)
    logger.info("Tasks DB: %s", tasks_db)
    if moved:
        for src, dst in moved:
            logger.info("Backed up %s -> %s", src, dst)
    else:
        logger.info("No existing task DB files found to back up")
    logger.info("Task schema recovery result: %s", result.get("status"))
    print(f"Data dir: {data_dir}")
    print(f"Tasks DB: {tasks_db}")
    print(f"Backup dir: {backup_dir if moved else '(none; no files existed)'}")
    print(f"Task schema: {result.get('status')}")
    return 0


def _reset_all_local_dbs() -> int:
    data_dir = _row_bot_data_dir()
    backup_dir = data_dir / "recovery" / f"local-db-reset-{_timestamp()}"
    dbs = [
        get_tasks_db_path(),
        get_memory_db_path(),
        get_threads_db_path(),
        get_access_db_path(),
    ]
    moved: list[tuple[Path, Path]] = []
    for db_path in dbs:
        moved.extend(_backup_family(db_path, backup_dir))
    from row_bot.tasks import ensure_task_schema

    result = ensure_task_schema(repair=True, force=True)
    logger.info("Data paths: %s", describe_data_paths())
    if moved:
        for src, dst in moved:
            logger.info("Backed up %s -> %s", src, dst)
    else:
        logger.info("No known local SQLite files found to back up")
    print(f"Data dir: {data_dir}")
    print(f"Backup dir: {backup_dir if moved else '(none; no files existed)'}")
    for db_path in dbs:
        print(f"Reset target: {db_path}")
    print(f"Task schema: {result.get('status')}")
    print("Memory, thread, and access databases will be recreated on next app startup.")
    return 0


def _restore_data(selector: str | None = None) -> int:
    data_dir = _row_bot_data_dir()
    recovery_root = data_dir / "recovery"
    if not recovery_root.exists():
        print(f"No restorable backup found under {recovery_root}")
        return 1
    if selector and selector != "latest":
        source_dir = Path(selector).expanduser()
        if not source_dir.is_absolute():
            source_dir = recovery_root / selector
    else:
        candidates = [p for p in recovery_root.iterdir() if p.is_dir()]
        source_dir = (
            max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
        )
    if not source_dir or not source_dir.exists():
        print(f"No restorable backup found under {recovery_root}")
        return 1

    pre_restore = recovery_root / f"pre-restore-{_timestamp()}"
    restored: list[tuple[Path, Path]] = []
    for name in (
        "tasks.db",
        "tasks.db-wal",
        "tasks.db-shm",
        "memory.db",
        "memory.db-wal",
        "memory.db-shm",
        "threads.db",
        "threads.db-wal",
        "threads.db-shm",
        "mobile.db",
        "mobile.db-wal",
        "mobile.db-shm",
    ):
        src = source_dir / name
        if not src.exists():
            continue
        dst = data_dir / name
        if dst.exists():
            pre_restore.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(pre_restore / name))
        shutil.copy2(src, dst)
        restored.append((src, dst))
    if not restored:
        print(f"No SQLite backup files found in {source_dir}")
        return 1
    print(f"Data dir: {data_dir}")
    print(f"Restored from: {source_dir}")
    if pre_restore.exists():
        print(f"Previous current files backed up to: {pre_restore}")
    for src, dst in restored:
        print(f"Restored {src} -> {dst}")
    if any(dst.name == "tasks.db" for _, dst in restored):
        from row_bot.tasks import ensure_task_schema

        result = ensure_task_schema(repair=True, force=True)
        print(f"Task schema after restore: {result.get('status')}")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Launch {APP_DISPLAY_NAME}")
    subparsers = parser.add_subparsers(dest="command")
    from row_bot.access.cli import add_remote_access_subcommands

    add_remote_access_subcommands(subparsers)
    plugins_parser = subparsers.add_parser("plugins", help="Plugin developer utilities")
    plugin_sub = plugins_parser.add_subparsers(dest="plugins_command", required=True)
    validate_parser = plugin_sub.add_parser(
        "validate", help="Validate a plugin directory"
    )
    validate_parser.add_argument("path")
    link_parser = plugin_sub.add_parser("link", help="Link a local plugin directory")
    link_parser.add_argument("path")
    reload_parser = plugin_sub.add_parser(
        "reload", help="Reload one linked or installed plugin"
    )
    reload_parser.add_argument("plugin_id")
    doctor_parser = plugin_sub.add_parser(
        "doctor", help="Validate setup and required config for a plugin"
    )
    doctor_parser.add_argument("plugin")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--browser",
        action="store_true",
        help=f"Open {APP_DISPLAY_NAME} in the system browser",
    )
    mode.add_argument(
        "--native",
        action="store_true",
        help=f"Open {APP_DISPLAY_NAME} in a pywebview native window",
    )
    parser.add_argument(
        "--tray", action="store_true", help="Force the system tray launcher"
    )
    parser.add_argument(
        "--no-tray", action="store_true", help="Run without a system tray icon"
    )
    parser.add_argument(
        "--server", action="store_true", help="Run the server without tray integration"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open a browser or native window"
    )
    parser.add_argument(
        "--no-splash", action="store_true", help="Skip the launcher splash screen"
    )
    parser.add_argument(
        "--no-ollama", action="store_true", help="Do not try to auto-start Ollama"
    )
    parser.add_argument(
        "--reset-tasks-db",
        action="store_true",
        help="Back up and recreate tasks.db before launch",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Back up known local SQLite DBs before launch",
    )
    parser.add_argument(
        "--restore-data",
        nargs="?",
        const="latest",
        default=None,
        help="Restore known SQLite DBs from a recovery backup directory or latest backup",
    )
    parser.add_argument(
        "--port", type=int, default=_PORT, help=f"Preferred app port (default: {_PORT})"
    )
    parser.add_argument(
        "--host", default=None, help="Host/interface for the NiceGUI server"
    )
    return parser


def quit_for_update() -> None:
    """Best-effort shutdown hook used by updater.install_and_restart."""
    global _ACTIVE_TRAY
    if _ACTIVE_TRAY is not None:
        try:
            _ACTIVE_TRAY._on_quit()
            return
        except Exception:
            pass
    os._exit(0)


# ── Main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    global _ACTIVE_TRAY, _EARLY_SPLASH_PROC
    _ensure_launcher_file_logging()
    _launch_event(
        "process_start",
        pid=os.getpid(),
        python=sys.executable,
        prefix=sys.prefix,
        cwd=os.getcwd(),
        frozen=getattr(sys, "frozen", False),
    )
    args = _build_arg_parser().parse_args(argv)
    if getattr(args, "command", "") == "access":
        from row_bot.access.cli import dispatch_access_command

        raise SystemExit(dispatch_access_command(args))
    if getattr(args, "command", "") == "plugins":
        from row_bot.plugins import devtools as plugin_devtools

        raise SystemExit(plugin_devtools.run_cli(args))
    if getattr(args, "command", "") == "serve":
        from row_bot.access.access_routes import AccessRouteConfigStore
        from row_bot.access.cli import resolve_serve_options, serve_startup_lines

        saved_routes = AccessRouteConfigStore().load_or_default()
        durable = {"host": ("0.0.0.0" if saved_routes.lan_enabled else "127.0.0.1")}
        try:
            serve_options = resolve_serve_options(
                args,
                durable=durable,
            )
        except ValueError as exc:
            raise SystemExit(f"row-bot serve: {exc}") from exc
        os.environ.update(serve_options.to_environment())
        for line in serve_startup_lines(serve_options):
            logger.info("%s", line)
        args.host = serve_options.host
        args.port = serve_options.port
        args.server = True
        args.no_open = True
        args.no_tray = True
        args.tray = False
        args.no_splash = True
        args.no_ollama = not serve_options.auto_start_ollama
        args.browser = False
        args.native = False
        args.reset_tasks_db = False
        args.reset_db = False
        args.restore_data = None
    args._dynamic_host_input = args.host
    args.host = _resolve_launch_host(args.host)
    preferred_mode = "browser" if args.browser else "native" if args.native else None
    linux_default_direct = sys.platform.startswith("linux") and not args.tray
    direct = args.server or args.no_tray or linux_default_direct
    can_show_early_splash = (
        not args.no_splash
        and not args.server
        and not args.no_open
        and not args.reset_tasks_db
        and not args.reset_db
        and args.restore_data is None
        and _has_display_server()
    )
    if can_show_early_splash:
        splash_started = time.perf_counter()
        _EARLY_SPLASH_PROC = _show_splash(parse_app_port(args.port, default=_PORT))
        _launch_event(
            "early_splash_requested",
            port=parse_app_port(args.port, default=_PORT),
            duration_ms=round((time.perf_counter() - splash_started) * 1000.0, 1),
            mode=preferred_mode or ("direct" if direct else "auto"),
        )
    if args.reset_tasks_db:
        raise SystemExit(_reset_tasks_db())
    if args.reset_db:
        raise SystemExit(_reset_all_local_dbs())
    if args.restore_data is not None:
        raise SystemExit(_restore_data(args.restore_data))

    try:
        if direct:
            if sys.platform.startswith("linux") and preferred_mode is None:
                args.browser = True
            _run_direct(args)
            return
        tray = RowBotTray(
            preferred_port=parse_app_port(args.port, default=_PORT),
            host=args.host,
            preferred_mode=preferred_mode,
            no_splash=args.no_splash,
            no_ollama=args.no_ollama,
        )
        _ACTIVE_TRAY = tray
        tray.run()
    except ImportError as exc:
        logger.warning(
            "System tray unavailable (%s); falling back to browser mode", exc
        )
        args.browser = True
        args.no_tray = True
        _run_direct(args)
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
