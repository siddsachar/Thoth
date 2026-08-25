"""State, projection, and native helpers for the torn-off Buddy surface.

This module deliberately contains no NiceGUI or pywebview imports.  The UI and
native host both use these small behavior seams, which keeps the important
placement, routing, positioning, approval, and foreground rules deterministic
under test.
"""

from __future__ import annotations

import html
import math
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping, Protocol


OVERLAY_WIDTH = 380
OVERLAY_HEIGHT = 230
OVERLAY_EDGE_MARGIN = 8


class BuddyPlacement(StrEnum):
    DOCKED = "docked"
    DESKTOP = "desktop"


class RuntimeSurface(StrEnum):
    CHAT = "normal_chat"
    DEVELOPER = "developer"
    DESIGNER = "designer"


@dataclass(frozen=True)
class BuddyPlacementState:
    """Canonical Buddy placement plus its independent presentation flags."""

    placement: BuddyPlacement = BuddyPlacement.DOCKED
    visible: bool = True
    collapsed: bool = False

    def tear_off(self) -> "BuddyPlacementState":
        return replace(self, placement=BuddyPlacement.DESKTOP, visible=True)

    def dock(self) -> "BuddyPlacementState":
        return BuddyPlacementState(BuddyPlacement.DOCKED, True, False)

    def hide(self) -> "BuddyPlacementState":
        return replace(self, visible=False)

    def show(self) -> "BuddyPlacementState":
        return replace(self, visible=True)

    def collapse(self) -> "BuddyPlacementState":
        if self.placement is not BuddyPlacement.DESKTOP:
            return self
        return replace(self, collapsed=True)

    def expand(self) -> "BuddyPlacementState":
        return replace(self, collapsed=False)


def placement_state_from_config(config: Mapping[str, Any] | None) -> BuddyPlacementState:
    cfg = config or {}
    raw = str(cfg.get("placement") or "").strip().lower()
    if raw not in {item.value for item in BuddyPlacement}:
        legacy_desktop = bool(cfg.get("desktop_enabled")) or str(cfg.get("mode") or "") == "desktop"
        raw = BuddyPlacement.DESKTOP.value if legacy_desktop else BuddyPlacement.DOCKED.value
    placement = BuddyPlacement(raw)
    visible = bool(cfg.get("visible", cfg.get("enabled", True)))
    collapsed = bool(cfg.get("collapsed", False)) if placement is BuddyPlacement.DESKTOP else False
    return BuddyPlacementState(placement, visible, collapsed)


def apply_placement_state(config: Mapping[str, Any], state: BuddyPlacementState) -> dict[str, Any]:
    """Return a canonical config update without reviving legacy surface flags."""

    updated = dict(config)
    updated.update(
        {
            "placement": state.placement.value,
            "visible": bool(state.visible),
            "collapsed": bool(state.collapsed and state.placement is BuddyPlacement.DESKTOP),
            "mode": "desktop" if state.placement is BuddyPlacement.DESKTOP else "sidebar",
        }
    )
    return updated


def should_cancel_main_close(config: Mapping[str, Any] | None) -> bool:
    state = placement_state_from_config(config)
    return state.placement is BuddyPlacement.DESKTOP


def should_defer_native_show(*, ready: bool, manual: bool) -> bool:
    """Only page-driven automatic shows wait for the ready handshake.

    A tray or control-server recovery request must always call the native
    window's ``show`` method, even if the page-ready bridge was missed.
    """

    return not bool(ready) and not bool(manual)


class NativeBuddyLifecycle:
    """Deterministic native-window transitions used by the pywebview host."""

    def __init__(
        self,
        *,
        load_config: Callable[[], dict[str, Any]],
        save_config: Callable[[dict[str, Any]], dict[str, Any]],
        main_window: Any,
        buddy_window: Callable[[], Any | None],
    ) -> None:
        self.load_config = load_config
        self.save_config = save_config
        self.main_window = main_window
        self.buddy_window = buddy_window

    def _save_state(self, state: BuddyPlacementState) -> dict[str, Any]:
        return self.save_config(apply_placement_state(self.load_config(), state))

    def tear_off(self, x: int, y: int) -> bool:
        config = self.load_config()
        state = placement_state_from_config(config).tear_off()
        overlay = dict(config.get("overlay") or {})
        overlay.update({"x": int(x), "y": int(y)})
        updated = apply_placement_state(config, state)
        updated["overlay"] = overlay
        self.save_config(updated)
        window = self.buddy_window()
        if window is None:
            return True
        try:
            window.move(int(x), int(y))
            window.show()
            return True
        except Exception:
            return False

    def dock(self) -> bool:
        self._save_state(placement_state_from_config(self.load_config()).dock())
        window = self.buddy_window()
        if window is not None:
            try:
                window.destroy()
            except Exception:
                return False
        return True

    def hide(self) -> bool:
        state = placement_state_from_config(self.load_config())
        if state.placement is not BuddyPlacement.DESKTOP:
            return False
        self._save_state(state.hide())
        window = self.buddy_window()
        if window is not None:
            try:
                window.hide()
            except Exception:
                return False
        return True

    def show(self) -> bool:
        state = placement_state_from_config(self.load_config())
        if state.placement is not BuddyPlacement.DESKTOP:
            return False
        self._save_state(state.show())
        window = self.buddy_window()
        if window is None:
            return True
        try:
            window.show()
            return True
        except Exception:
            return False

    def main_closing(self) -> bool:
        """Return False to cancel pywebview close while Buddy is torn off."""

        if not should_cancel_main_close(self.load_config()):
            return True
        try:
            self.main_window.hide()
        except Exception:
            return True
        return False

    def moved(self, x: int, y: int) -> dict[str, Any]:
        config = self.load_config()
        overlay = dict(config.get("overlay") or {})
        overlay.update({"x": int(x), "y": int(y)})
        config["overlay"] = overlay
        return self.save_config(config)

    def quit(self) -> None:
        window = self.buddy_window()
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass
        try:
            self.main_window.destroy()
        except Exception:
            pass


@dataclass(frozen=True)
class ScreenArea:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + max(0, self.width)

    @property
    def bottom(self) -> int:
        return self.y + max(0, self.height)

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def distance_squared(self, x: float, y: float) -> float:
        near_x = min(max(x, self.x), self.right)
        near_y = min(max(y, self.y), self.bottom)
        return (x - near_x) ** 2 + (y - near_y) ** 2


def _valid_screens(screens: Iterable[ScreenArea]) -> list[ScreenArea]:
    return [screen for screen in screens if screen.width > 0 and screen.height > 0]


def nearest_screen(x: float, y: float, screens: Iterable[ScreenArea]) -> ScreenArea:
    available = _valid_screens(screens)
    if not available:
        return ScreenArea(0, 0, 1920, 1080)
    for screen in available:
        if screen.contains(x, y):
            return screen
    return min(available, key=lambda screen: screen.distance_squared(x, y))


def clamp_overlay_position(
    x: float,
    y: float,
    screens: Iterable[ScreenArea],
    *,
    width: int = OVERLAY_WIDTH,
    height: int = OVERLAY_HEIGHT,
    margin: int = OVERLAY_EDGE_MARGIN,
) -> tuple[int, int]:
    """Clamp a window origin to the nearest visible work area.

    Negative coordinates are intentionally retained when the chosen monitor is
    left of, or above, the primary monitor.
    """

    screen = nearest_screen(x + width / 2, y + height / 2, screens)
    min_x = screen.x + margin
    min_y = screen.y + margin
    max_x = max(min_x, screen.right - width - margin)
    max_y = max(min_y, screen.bottom - height - margin)
    return (
        int(round(min(max(x, min_x), max_x))),
        int(round(min(max(y, min_y), max_y))),
    )


def position_for_drop(
    screen_x: float,
    screen_y: float,
    screens: Iterable[ScreenArea],
    *,
    width: int = OVERLAY_WIDTH,
    height: int = OVERLAY_HEIGHT,
) -> tuple[int, int]:
    """Centre the overlay around a dock drop point and keep it visible."""

    return clamp_overlay_position(
        screen_x - width / 2,
        screen_y - min(height / 3, 76),
        screens,
        width=width,
        height=height,
    )


def runtime_surface_for_state(state: Any) -> RuntimeSurface:
    if getattr(state, "active_developer_workspace_id", None):
        return RuntimeSurface.DEVELOPER
    if getattr(state, "active_designer_project", None):
        return RuntimeSurface.DESIGNER
    return RuntimeSurface.CHAT


@dataclass(frozen=True)
class OverlayTurnTarget:
    """The immutable thread/runtime identity captured when Send is pressed."""

    thread_id: str
    thread_name: str
    runtime_surface: RuntimeSurface
    developer_workspace_id: str = ""
    designer_project_id: str = ""
    designer_mode: str = ""
    model_override: str = ""
    approval_mode: str = ""
    messages: list[Any] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def capture(cls, state: Any) -> "OverlayTurnTarget":
        project = getattr(state, "active_designer_project", None)
        return cls(
            thread_id=str(getattr(state, "thread_id", "") or ""),
            thread_name=str(getattr(state, "thread_name", "") or ""),
            runtime_surface=runtime_surface_for_state(state),
            developer_workspace_id=str(getattr(state, "active_developer_workspace_id", "") or ""),
            designer_project_id=str(getattr(project, "id", "") or ""),
            designer_mode=str(getattr(project, "mode", "") or ""),
            model_override=str(getattr(state, "thread_model_override", "") or ""),
            approval_mode=str(getattr(state, "thread_approval_mode", "") or ""),
            messages=getattr(state, "messages", None),
        )

    def configurable_values(self) -> dict[str, str]:
        values = {"runtime_surface": self.runtime_surface.value}
        if self.developer_workspace_id:
            values["developer_workspace_id"] = self.developer_workspace_id
        if self.designer_project_id:
            values["designer_project_id"] = self.designer_project_id
        if self.designer_mode:
            values["designer_mode"] = self.designer_mode
        if self.model_override:
            values["model_override"] = self.model_override
        if self.approval_mode:
            values["approval_mode"] = self.approval_mode
        return values


_HTML_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_FENCE = re.compile(r"^\s*```[^\n]*$", re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_LIST_PREFIX = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+", re.MULTILINE)


def plain_text_projection(value: Any) -> str:
    """Create a compact, safe plain-text projection without rendering Markdown."""

    text = str(value or "")
    text = _MARKDOWN_IMAGE.sub(lambda match: match.group(1), text)
    text = _MARKDOWN_LINK.sub(lambda match: match.group(1), text)
    text = _FENCE.sub("", text)
    text = _HEADING.sub("", text)
    text = _LIST_PREFIX.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()


@dataclass(frozen=True)
class ApprovalProjection:
    required: bool = False
    simple: bool = False
    description: str = ""
    reason: str = ""
    count: int = 0


def project_approval(pending: Any) -> ApprovalProjection:
    if pending is None:
        return ApprovalProjection()
    items = pending if isinstance(pending, list) else [pending]
    items = [item for item in items if item is not None]
    if not items:
        return ApprovalProjection()
    item = items[0]
    if len(items) != 1 or not isinstance(item, Mapping):
        return ApprovalProjection(True, False, "Approval required", "Review in Row-Bot", len(items))
    description = plain_text_projection(
        item.get("description")
        or item.get("expected_effect")
        or item.get("label")
        or item.get("reason")
    )
    reason = plain_text_projection(item.get("reason") or item.get("target") or item.get("data_summary"))
    sufficiently_described = bool(description and (reason or item.get("reversible") is not None))
    if not sufficiently_described:
        return ApprovalProjection(True, False, "Approval required", "Review in Row-Bot", 1)
    return ApprovalProjection(True, True, description[:240], reason[:240], 1)


def _latest_assistant_text(messages: Iterable[Any]) -> str:
    for message in reversed(list(messages or [])):
        if not isinstance(message, Mapping) or str(message.get("role") or "") != "assistant":
            continue
        content = plain_text_projection(message.get("content"))
        if content:
            return content
    return ""


def _generation_progress(generation: Any, buddy_status: str) -> str:
    pending_tools = getattr(generation, "pending_tools", None)
    if isinstance(pending_tools, Mapping) and pending_tools:
        latest = next(reversed(pending_tools.values()))
        if isinstance(latest, Mapping):
            label = latest.get("label") or latest.get("name") or latest.get("tool")
            if label:
                return f"Working with {plain_text_projection(label)}"
    return plain_text_projection(buddy_status) or "Thinking…"


@dataclass(frozen=True)
class BuddyThreadSnapshot:
    thread_id: str = ""
    thread_name: str = "New chat"
    runtime_surface: RuntimeSurface = RuntimeSurface.CHAT
    response_text: str = ""
    progress_text: str = ""
    generating: bool = False
    can_stop: bool = False
    approval: ApprovalProjection = ApprovalProjection()
    error: str = ""

    @property
    def key(self) -> tuple[Any, ...]:
        return (
            self.thread_id,
            self.thread_name,
            self.runtime_surface.value,
            self.response_text,
            self.progress_text,
            self.generating,
            self.can_stop,
            self.approval,
            self.error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "thread_name": self.thread_name,
            "runtime_surface": self.runtime_surface.value,
            "response_text": self.response_text,
            "progress_text": self.progress_text,
            "generating": self.generating,
            "can_stop": self.can_stop,
            "approval": {
                "required": self.approval.required,
                "simple": self.approval.simple,
                "description": self.approval.description,
                "reason": self.approval.reason,
                "count": self.approval.count,
            },
            "error": self.error,
        }


def build_thread_snapshot(
    state: Any,
    active_generations: Mapping[str, Any],
    *,
    buddy_status: str = "",
) -> BuddyThreadSnapshot:
    """Project only the selected thread from shared application state."""

    thread_id = str(getattr(state, "thread_id", "") or "")
    generation = active_generations.get(thread_id) if thread_id else None
    generation_status = str(getattr(generation, "status", "") or "")
    generating = generation is not None and generation_status in {"streaming", "interrupted"}
    answer = plain_text_projection(getattr(generation, "accumulated", "")) if generation else ""
    if not answer:
        answer = _latest_assistant_text(getattr(state, "messages", None) or [])
    pending = getattr(generation, "interrupt_data", None) if generation else None
    pending_generation_id = str(getattr(state, "pending_interrupt_generation_id", "") or "")
    if pending is None and thread_id and pending_generation_id.startswith(f"{thread_id}:"):
        pending = getattr(state, "pending_interrupt", None)
    error = plain_text_projection(getattr(generation, "error", "")) if generation else ""
    return BuddyThreadSnapshot(
        thread_id=thread_id,
        thread_name=str(getattr(state, "thread_name", "") or "New chat"),
        runtime_surface=runtime_surface_for_state(state),
        response_text=answer,
        progress_text="" if answer or not generating else _generation_progress(generation, buddy_status),
        generating=generating,
        can_stop=generating,
        approval=project_approval(pending),
        error=error,
    )


@dataclass(frozen=True)
class ForegroundWindow:
    handle: Any
    process_id: int
    title: str = ""
    app_name: str = ""


class ForegroundBackend(Protocol):
    def current(self) -> ForegroundWindow | None: ...

    def activate(self, window: ForegroundWindow) -> bool: ...


class ForegroundAppTracker:
    """Remember and restore the last foreground app not owned by Row-Bot."""

    def __init__(
        self,
        backend: ForegroundBackend,
        *,
        own_process_id: int | None = None,
        ignored_handles: Callable[[], Iterable[Any]] | None = None,
        ignored_titles: Iterable[str] = (),
    ) -> None:
        self.backend = backend
        self.own_process_id = int(own_process_id if own_process_id is not None else os.getpid())
        self.ignored_handles = ignored_handles or (lambda: ())
        self.ignored_titles = {str(title).strip().casefold() for title in ignored_titles}
        self.last_external: ForegroundWindow | None = None

    def _is_owned(self, window: ForegroundWindow) -> bool:
        if window.process_id == self.own_process_id:
            return True
        if window.handle in set(self.ignored_handles()):
            return True
        title = window.title.strip().casefold()
        return any(ignored and ignored in title for ignored in self.ignored_titles)

    def observe(self) -> ForegroundWindow | None:
        window = self.backend.current()
        if window is not None and not self._is_owned(window):
            self.last_external = window
        return self.last_external

    def restore_once(self) -> bool:
        target = self.last_external
        return bool(target is not None and self.backend.activate(target))

    @property
    def app_name(self) -> str:
        target = self.last_external
        return str((target.app_name or target.title) if target else "").strip()


class _NullForegroundBackend:
    def current(self) -> ForegroundWindow | None:
        return None

    def activate(self, window: ForegroundWindow) -> bool:  # noqa: ARG002
        return False


class _WindowsForegroundBackend:
    def __init__(self) -> None:
        import ctypes

        self.ctypes = ctypes
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

    def _app_name(self, process_id: int) -> str:
        access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        handle = self.kernel32.OpenProcess(access, False, process_id)
        if not handle:
            return ""
        try:
            size = self.ctypes.c_ulong(32768)
            buffer = self.ctypes.create_unicode_buffer(size.value)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, self.ctypes.byref(size)):
                return pathlib.Path(buffer.value).stem
        finally:
            self.kernel32.CloseHandle(handle)
        return ""

    def current(self) -> ForegroundWindow | None:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return None
        process_id = self.ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(process_id))
        length = max(0, int(self.user32.GetWindowTextLengthW(hwnd)))
        buffer = self.ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return ForegroundWindow(hwnd, int(process_id.value), buffer.value, self._app_name(int(process_id.value)))

    def activate(self, window: ForegroundWindow) -> bool:
        if not window.handle or not self.user32.IsWindow(window.handle):
            return False
        self.user32.ShowWindow(window.handle, 9)  # SW_RESTORE
        return bool(self.user32.SetForegroundWindow(window.handle))


class _MacForegroundBackend:
    def current(self) -> ForegroundWindow | None:
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None
            pid = int(app.processIdentifier())
            name = str(app.localizedName() or "")
            return ForegroundWindow(pid, pid, name, name)
        except Exception:
            return None

    def activate(self, window: ForegroundWindow) -> bool:
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(window.process_id)
            return bool(app and app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
        except Exception:
            return False


def platform_foreground_backend() -> ForegroundBackend:
    if sys.platform == "win32":
        try:
            return _WindowsForegroundBackend()
        except Exception:
            return _NullForegroundBackend()
    if sys.platform == "darwin":
        return _MacForegroundBackend()
    return _NullForegroundBackend()


def screen_areas_from_native(screens: Iterable[Any]) -> list[ScreenArea]:
    """Normalise pywebview screens, preferring a native work-area frame."""

    result: list[ScreenArea] = []
    for screen in screens or ():
        try:
            frame = getattr(screen, "frame", None)
            # WinForms exposes Screen.WorkingArea through pywebview's frame
            # field. Cocoa currently exposes a full NSRect instead, so it
            # safely falls through to pywebview's coordinate fields below.
            if frame is not None and all(
                hasattr(frame, name) for name in ("X", "Y", "Width", "Height")
            ):
                result.append(
                    ScreenArea(
                        int(frame.X),
                        int(frame.Y),
                        int(frame.Width),
                        int(frame.Height),
                    )
                )
                continue
            result.append(
                ScreenArea(
                    int(getattr(screen, "x")),
                    int(getattr(screen, "y")),
                    int(getattr(screen, "width")),
                    int(getattr(screen, "height")),
                )
            )
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def finite_coordinate(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback
