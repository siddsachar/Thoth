"""Deterministic fake Cua MCP transport for Computer Use tests."""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass, field
from typing import Any

from row_bot.mcp_client.results import RawCallContent, RawCallResult


_ONE_PIXEL_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")

_CALCULATOR_BUTTON_LABELS = (
    "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Decimal separator", "Plus", "Minus", "Multiply by", "Divide by", "Percent", "Equals",
    "Open parenthesis", "Close parenthesis",
)
_CALCULATOR_LABEL_TO_KEY = {
    "Zero": "0", "One": "1", "Two": "2", "Three": "3", "Four": "4",
    "Five": "5", "Six": "6", "Seven": "7", "Eight": "8", "Nine": "9",
    "Decimal separator": ".", "Plus": "+", "Minus": "-", "Multiply by": "*",
    "Divide by": "/", "Percent": "%", "Equals": "=", "Open parenthesis": "(",
    "Close parenthesis": ")",
}

SANITIZED_NATIVE_BROWSER_APPS = (
    {"name": "msedge.exe", "running": True, "active": True},
    {"name": "Notepad", "running": True, "active": False},
)

SANITIZED_NATIVE_BROWSER_WINDOWS = (
    {
        "window_id": 501,
        "pid": 2501,
        "app_name": "msedge.exe",
        "title": "Example media - Microsoft Edge",
        "bounds": {"x": 0, "y": 0, "width": 1280, "height": 720},
        "is_on_screen": True,
    },
)


@dataclass
class FakeScenario:
    stale: bool = False
    disconnect: bool = False
    permission_denied: bool = False
    malformed_image: bool = False
    oversized_tree: bool = False
    effect: str = "confirmed"
    delivery_mode: str = "background"
    injection_label: str = ""
    calculator_semantics: bool = False
    windows: tuple[dict[str, Any], ...] = ()
    apps: tuple[dict[str, Any], ...] = ()
    list_apps_error_code: str = ""
    launch_error_code: str = ""
    action_error_code: str = ""
    capture_pid: int = 0
    capture_window_id: int = 0
    capture_images: tuple[str, ...] = ()
    capture_dimensions: tuple[int, int] = (1, 1)
    include_scale_factor: bool = True
    element_frame: tuple[float, float, float, float] = (0, 0, 1, 1)
    background_unavailable_tools: frozenset[str] = field(default_factory=frozenset)
    foreground_effect: str = "unverifiable"
    document_value: str = ""
    block_foreground: bool = False
    semantic_elements: tuple[dict[str, Any], ...] = ()
    accepted_background_noop_tools: frozenset[str] = field(default_factory=frozenset)


class FakeCuaTransport:
    """Small raw-result transport covering all Beta tools and failure classes."""

    def __init__(self, scenario: FakeScenario | None = None) -> None:
        self.scenario = scenario or FakeScenario()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.opened = False
        self.closed = False
        self.block_action = threading.Event()
        self.release_action = threading.Event()
        self.generation = 1
        self.pressed_keys: list[str] = []
        self.effective_keys: list[str] = []
        self.calculator_display = "0"
        self.element_labels: dict[str, str] = {}
        self.capture_index = 0
        self.document_value = self.scenario.document_value

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True
        self.release_action.set()

    def call_raw(self, name: str, arguments: dict[str, Any] | None = None) -> RawCallResult:
        args = dict(arguments or {})
        recorded = dict(args)
        if name == "type_text" and "text" in recorded:
            recorded["text"] = f"<redacted:{len(str(recorded['text']))} chars>"
        self.calls.append((name, recorded))
        if self.scenario.disconnect:
            self.scenario.disconnect = False
            self.generation += 1
            raise ConnectionError("fake transport disconnected")
        if name == "set_config":
            return self._result({"capture_scope": "window", "max_image_dimension": 1456})
        if name in {"start_session", "end_session"}:
            return self._result({"session": args.get("session"), "ok": True})
        if name == "health_report":
            overall = "failed" if self.scenario.permission_denied else "ok"
            return self._result({
                "schema_version": "1",
                "platform": "win32",
                "driver_version": "0.7.1",
                "overall": overall,
                "checks": [{
                    "name": "ax_capability",
                    "status": "fail" if self.scenario.permission_denied else "pass",
                    "message": "fake permission state",
                    "hint": "Grant accessibility permission" if self.scenario.permission_denied else "",
                }],
            })
        if name == "check_permissions":
            return self._result({"accessibility": not self.scenario.permission_denied, "screen_recording": True})
        if name == "list_apps":
            if self.scenario.list_apps_error_code:
                return self._error(
                    "fake app inventory warning",
                    self.scenario.list_apps_error_code,
                )
            apps = list(self.scenario.apps) if self.scenario.apps else [
                {"name": "Calculator", "pid": 4242, "running": True, "active": False},
                {"name": "Notepad", "pid": 4343, "running": True, "active": False},
            ]
            return self._result({"apps": apps})
        if name == "list_windows":
            windows = list(self.scenario.windows) if self.scenario.windows else [
                {"window_id": 101, "pid": 4242, "app_name": "Calculator", "title": "Calculator", "bounds": {"x": -100, "y": 20, "width": 800, "height": 600}, "is_on_screen": True},
                {"window_id": 102, "pid": 4343, "app_name": "Notepad", "title": "Untitled - Notepad", "bounds": {"x": 700, "y": 20, "width": 900, "height": 700}, "is_on_screen": True},
            ]
            return self._result({"windows": windows})
        if name == "launch_app":
            if self.scenario.launch_error_code:
                return self._error("fake launch failure", self.scenario.launch_error_code)
            return self._result({"pid": 4242, "name": str(args.get("name") or "Calculator"), "windows": [{"window_id": 101, "title": "Calculator"}]})
        if name == "get_window_state":
            if self.scenario.permission_denied:
                return self._error("permission denied", "permission_denied")
            if self.scenario.semantic_elements:
                element_specs = [
                    (
                        str(item.get("role") or "text"),
                        str(item.get("label") or ""),
                        int(item.get("depth") or 1),
                    )
                    for item in self.scenario.semantic_elements
                ]
            elif self.scenario.calculator_semantics and not self.scenario.oversized_tree:
                element_specs = [("text", f"Display {self.calculator_display}")] + [
                    ("button", label) for label in _CALCULATOR_BUTTON_LABELS
                ]
            else:
                count = 300 if self.scenario.oversized_tree else 3
                element_specs = [
                    (
                        "button" if index != 2 else "text_field",
                        self.scenario.injection_label
                        if index == 0 and self.scenario.injection_label
                        else f"Display {self.calculator_display}"
                        if index == 0
                        else "Equals"
                        if index == 1
                        else "Input"
                        if index == 2
                        else f"Digit {index}",
                    )
                    for index in range(count)
                ]
            elements = []
            self.element_labels = {}
            for index, element_spec in enumerate(element_specs):
                role, label = element_spec[:2]
                depth = int(element_spec[2]) if len(element_spec) > 2 else (
                    index if self.scenario.oversized_tree else 1
                )
                token = f"g{self.generation}-element-{index}"
                self.element_labels[token] = label
                elements.append({
                    "element_index": index,
                    "element_token": token,
                    "role": role,
                    "label": label,
                    "value": self.document_value if index == 2 else "",
                    "frame": {
                        "x": self.scenario.element_frame[0],
                        "y": self.scenario.element_frame[1],
                        "w": self.scenario.element_frame[2],
                        "h": self.scenario.element_frame[3],
                    },
                    "depth": depth,
                })
            if self.scenario.malformed_image:
                image = "not-base64"
            elif self.scenario.capture_images:
                image = self.scenario.capture_images[
                    min(self.capture_index, len(self.scenario.capture_images) - 1)
                ]
            else:
                image = _ONE_PIXEL_PNG
            self.capture_index += 1
            width, height = self.scenario.capture_dimensions
            structured = {
                "schema_version": "1",
                "pid": self.scenario.capture_pid or args.get("pid", 4242),
                "window_id": self.scenario.capture_window_id or args.get("window_id", 101),
                "screenshot_width": width,
                "screenshot_height": height,
                "elements": elements,
            }
            if self.scenario.include_scale_factor:
                structured["scale_factor"] = 1.25
            return RawCallResult(
                content=(
                    RawCallContent(kind="text", text="fake window state"),
                    RawCallContent(kind="image", data=image, mime_type="image/png"),
                ),
                structured_content=structured,
            )
        if name in {"click", "double_click", "right_click", "type_text", "press_key", "hotkey", "scroll", "drag", "bring_to_front"}:
            delivery_mode = str(args.get("delivery_mode") or "background")
            accepted_noop = bool(
                name in self.scenario.accepted_background_noop_tools
                and delivery_mode != "foreground"
            )
            if self.block_action.is_set() and (
                not self.scenario.block_foreground
                or delivery_mode == "foreground"
            ):
                self.release_action.wait(timeout=5)
            if self.scenario.stale:
                self.scenario.stale = False
                return self._error("element token is stale", "stale_element")
            if self.scenario.action_error_code:
                return self._error(
                    "fake action failure",
                    self.scenario.action_error_code,
                )
            if name in self.scenario.background_unavailable_tools and delivery_mode != "foreground":
                return self._top_level_error(
                    "Background delivery is unavailable for this target.",
                    "background_unavailable",
                )
            if name == "type_text":
                typed = str(args.get("text") or "")
                if args.get("element_token") or args.get("element_index") is not None:
                    # Pinned Cua Windows behavior: UIA ValuePattern.SetValue is
                    # an atomic whole-value replacement, not caret insertion.
                    self.document_value = typed
                    return self._result({
                        "path": "ax",
                        "effect": "confirmed",
                        "verified": True,
                        "delivery_mode": delivery_mode,
                    })
                self.document_value += typed
                effect = (
                    self.scenario.foreground_effect
                    if delivery_mode == "foreground"
                    else self.scenario.effect
                )
                return self._result({
                    "path": "key_events",
                    "effect": effect,
                    "verified": effect == "confirmed",
                    "delivery_mode": delivery_mode,
                })
            if name == "press_key":
                key = str(args.get("key") or "")
                self.pressed_keys.append(key)
                if not accepted_noop:
                    self.effective_keys.append(key)
            elif name == "click":
                label = self.element_labels.get(str(args.get("element_token") or ""), "")
                key = _CALCULATOR_LABEL_TO_KEY.get(label)
                if key:
                    self.pressed_keys.append(key)
                    self.effective_keys.append(key)
            if self.pressed_keys[-4:] == ["7", "*", "8", "="]:
                self.calculator_display = "56"
            effect = "unverifiable" if accepted_noop else (
                self.scenario.foreground_effect
                if delivery_mode == "foreground"
                else self.scenario.effect
            )
            return self._result({
                "effect": effect,
                "verified": effect == "confirmed",
                "delivery_mode": delivery_mode,
                "escalation": "foreground" if delivery_mode == "foreground" else "",
            })
        return self._error(f"unknown fake tool: {name}", "unknown_tool")

    @staticmethod
    def _result(structured: dict[str, Any]) -> RawCallResult:
        return RawCallResult((RawCallContent(kind="text", text="ok"),), structured, False)

    @staticmethod
    def _error(message: str, code: str) -> RawCallResult:
        return RawCallResult((RawCallContent(kind="text", text=message),), {"error": {"code": code, "message": message}}, True)

    @staticmethod
    def _top_level_error(message: str, code: str) -> RawCallResult:
        return RawCallResult(
            (RawCallContent(kind="text", text=message),),
            {"error": True, "error_code": code, "message": message},
            True,
        )
