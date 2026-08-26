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


def _editable_role(role: object) -> bool:
    normalized = "".join(
        character for character in str(role or "").casefold() if character.isalnum()
    )
    return normalized in {
        "combobox",
        "edit",
        "entry",
        "input",
        "searchfield",
        "textarea",
        "textfield",
        "textinput",
        "textbox",
    }


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
    calculator_sparse_after_action: bool = False
    windows: tuple[dict[str, Any], ...] = ()
    window_snapshots: tuple[tuple[dict[str, Any], ...], ...] = ()
    apps: tuple[dict[str, Any], ...] = ()
    list_apps_error_code: str = ""
    launch_error_code: str = ""
    launch_pid: int = 4242
    launch_window_id: int = 101
    launch_bundle_id: str = ""
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
    semantic_snapshots: tuple[tuple[dict[str, Any], ...], ...] = ()
    accepted_background_noop_tools: frozenset[str] = field(default_factory=frozenset)
    action_route: str = ""
    action_cause: str = ""
    element_type_effect: str = "confirmed"
    driver_declared_count: int | None = None
    driver_limited: bool | None = None
    driver_sparse: bool = False
    verify_status: str = "satisfied"
    menu_error_code: str = ""
    close_target_after_labels: frozenset[str] = field(default_factory=frozenset)
    rotate_element_tokens: bool = False
    set_value_updates_document: bool = True
    action_error_message: str = "fake action failure"
    delivery_profile: str = "native_targeted_insertion"
    scale_factor: float = 1.25


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
        self.element_indexes: dict[str, int] = {}
        self.element_values: dict[int, str] = {}
        self.mutated_element_indexes: set[int] = set()
        self.capture_index = 0
        self.document_value = self.scenario.document_value
        self.window_snapshot_index = 0
        self.closed_targets: set[tuple[int, int]] = set()

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True
        self.release_action.set()

    def value_for_label(self, label: str) -> str:
        indexes = [
            index
            for token, index in self.element_indexes.items()
            if self.element_labels.get(token) == label
        ]
        if len(indexes) != 1:
            raise KeyError(label)
        return self.element_values.get(indexes[0], "")

    def call_raw(self, name: str, arguments: dict[str, Any] | None = None) -> RawCallResult:
        args = dict(arguments or {})
        recorded = dict(args)
        if name == "type_text" and "text" in recorded:
            recorded["text"] = f"<redacted:{len(str(recorded['text']))} chars>"
        if name == "set_value" and "value" in recorded:
            recorded["value"] = f"<redacted:{len(str(recorded['value']))} chars>"
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
        if name == "verify_state":
            target = (int(args.get("pid") or 0), int(args.get("window_id") or 0))
            status = (
                "unsatisfied"
                if target in self.closed_targets
                else self.scenario.verify_status
            )
            return self._result(
                {
                    "status": status,
                    "stable": status == "satisfied",
                    "elapsed_ms": 0,
                    "samples": 1,
                    "predicates": [
                        {
                            "index": 0,
                            "status": status,
                            "unknown_reason": None,
                            "observed_json": None,
                        }
                    ],
                }
            )
        if name == "invoke_menu":
            if self.scenario.menu_error_code:
                return self._error("synthetic menu refusal", self.scenario.menu_error_code)
            return self._result(
                {
                    "effect": "confirmed",
                    "route": "accessibility",
                    "delivery": {"mode": "foreground", "delivered_count": 1},
                    "verified": True,
                }
            )
        if name == "list_apps":
            if self.scenario.list_apps_error_code:
                return self._error(
                    "fake app inventory warning",
                    self.scenario.list_apps_error_code,
                )
            apps = list(self.scenario.apps) if self.scenario.apps else [
                {
                    "name": "Calculator",
                    "pid": 4242,
                    "running": True,
                    "active": False,
                    "kind": "uwp",
                    "bundle_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
                },
                {"name": "Notepad", "pid": 4343, "running": True, "active": False},
            ]
            return self._result({"apps": apps})
        if name == "list_windows":
            if self.scenario.window_snapshots:
                index = min(
                    self.window_snapshot_index,
                    len(self.scenario.window_snapshots) - 1,
                )
                windows = list(self.scenario.window_snapshots[index])
                self.window_snapshot_index += 1
            else:
                windows = list(self.scenario.windows) if self.scenario.windows else [
                    {"window_id": 101, "pid": 4242, "app_name": "Calculator", "title": "Calculator", "bounds": {"x": -100, "y": 20, "width": 800, "height": 600}, "is_on_screen": True},
                    {"window_id": 102, "pid": 4343, "app_name": "Notepad", "title": "Untitled - Notepad", "bounds": {"x": 700, "y": 20, "width": 900, "height": 700}, "is_on_screen": True},
                ]
            windows = [
                row
                for row in windows
                if (int(row.get("pid") or 0), int(row.get("window_id") or 0))
                not in self.closed_targets
            ]
            return self._result({"windows": windows})
        if name == "launch_app":
            if self.scenario.launch_error_code:
                return self._error("fake launch failure", self.scenario.launch_error_code)
            app_name = str(args.get("name") or "Calculator")
            window = {
                "window_id": self.scenario.launch_window_id,
                "title": "Calculator",
            }
            if self.scenario.calculator_semantics:
                window["app_name"] = app_name
            inventory = list(self.scenario.apps) if self.scenario.apps else [
                {
                    "name": "Calculator",
                    "bundle_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
                },
                {"name": "Notepad"},
            ]
            matched_app = next(
                (
                    row
                    for row in inventory
                    if str(row.get("name") or "").casefold() == app_name.casefold()
                ),
                {},
            )
            package_identity = self.scenario.launch_bundle_id or str(
                matched_app.get("bundle_id") or ""
            )
            if package_identity and "!" not in package_identity:
                package_identity = f"{package_identity}!App"
            return self._result({
                "pid": self.scenario.launch_pid,
                "name": package_identity or app_name,
                "bundle_id": package_identity,
                "windows": [window],
            })
        if name == "get_window_state":
            target = (int(args.get("pid") or 0), int(args.get("window_id") or 0))
            if target in self.closed_targets:
                return self._error("target window no longer exists", "target_not_found")
            if self.scenario.permission_denied:
                return self._error("permission denied", "permission_denied")
            if self.scenario.semantic_snapshots:
                snapshot_index = min(
                    self.capture_index,
                    len(self.scenario.semantic_snapshots) - 1,
                )
                element_specs = [
                    (
                        str(item.get("role") or "text"),
                        str(item.get("label") or ""),
                        int(item.get("depth") or 1),
                        item,
                    )
                    for item in self.scenario.semantic_snapshots[snapshot_index]
                ]
            elif self.scenario.semantic_elements:
                element_specs = [
                    (
                        str(item.get("role") or "text"),
                        str(item.get("label") or ""),
                        int(item.get("depth") or 1),
                        item,
                    )
                    for item in self.scenario.semantic_elements
                ]
            elif (
                self.scenario.calculator_semantics
                and not self.scenario.oversized_tree
                and not (
                    self.scenario.calculator_sparse_after_action
                    and self.pressed_keys
                )
            ):
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
            self.element_indexes = {}
            for index, element_spec in enumerate(element_specs):
                role, label = element_spec[:2]
                depth = int(element_spec[2]) if len(element_spec) > 2 else (
                    index if self.scenario.oversized_tree else 1
                )
                source = element_spec[3] if len(element_spec) > 3 else {}
                token_generation = (
                    f"g{self.generation}-s{self.capture_index}"
                    if self.scenario.rotate_element_tokens
                    else f"g{self.generation}"
                )
                token = f"{token_generation}-element-{index}"
                self.element_labels[token] = label
                self.element_indexes[token] = index
                source_frame = source.get("frame")
                frame = (
                    dict(source_frame)
                    if isinstance(source_frame, dict)
                    else {
                        "x": self.scenario.element_frame[0],
                        "y": self.scenario.element_frame[1],
                        "w": self.scenario.element_frame[2],
                        "h": self.scenario.element_frame[3],
                    }
                )
                default_value = (
                    self.document_value
                    if str(role).casefold()
                    in {
                        "cell",
                        "dataitem",
                        "edit",
                        "entry",
                        "gridcell",
                        "input",
                        "tablecell",
                        "textfield",
                        "textinput",
                        "textbox",
                        "text_field",
                    }
                    else ""
                )
                source_has_value = "value" in source
                if index not in self.mutated_element_indexes:
                    self.element_values[index] = str(
                        source.get("value", default_value) or ""
                    )
                exposed_value: str | None = self.element_values.get(index, "")
                if (
                    self.scenario.delivery_profile == "catalyst_value_unavailable"
                    and _editable_role(role)
                ):
                    exposed_value = None
                elif source_has_value and index not in self.mutated_element_indexes:
                    exposed_value = source.get("value")
                element = {
                    "element_index": index,
                    "element_token": token,
                    "role": role,
                    "label": label,
                    "value": exposed_value,
                    "frame": frame,
                    "depth": depth,
                }
                for key in (
                    "parent_index",
                    "visible",
                    "enabled",
                    "selected",
                    "checked",
                    "expanded",
                    "pressed",
                    "toggled",
                    "editable",
                    "read_only",
                    "in_web_content",
                ):
                    if key in source:
                        element[key] = source[key]
                elements.append(element)
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
                "element_count": (
                    self.scenario.driver_declared_count
                    if self.scenario.driver_declared_count is not None
                    else len(elements)
                ),
                "total_element_count": (
                    self.scenario.driver_declared_count
                    if self.scenario.driver_declared_count is not None
                    else len(elements)
                ),
                "returned_element_count": len(elements),
                "snapshot_id": f"g{self.generation}",
            }
            if self.scenario.driver_limited is not None:
                structured["truncated"] = self.scenario.driver_limited
            if self.scenario.driver_sparse:
                structured["degraded"] = True
            if self.scenario.include_scale_factor:
                structured["scale_factor"] = self.scenario.scale_factor
            content = [RawCallContent(kind="text", text="fake window state")]
            if args.get("include_screenshot") is not False:
                content.append(
                    RawCallContent(kind="image", data=image, mime_type="image/png")
                )
            else:
                structured.pop("screenshot_width", None)
                structured.pop("screenshot_height", None)
            return RawCallResult(
                content=tuple(content),
                structured_content=structured,
            )
        if name in {"click", "double_click", "right_click", "type_text", "set_value", "press_key", "hotkey", "scroll", "drag", "bring_to_front"}:
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
                    self.scenario.action_error_message,
                    self.scenario.action_error_code,
                )
            if (
                delivery_mode != "foreground"
                and (
                    name in self.scenario.background_unavailable_tools
                    or (
                        self.scenario.delivery_profile == "background_refused"
                        and name == "type_text"
                    )
                )
            ):
                return self._top_level_error(
                    "Background delivery is unavailable for this target.",
                    "background_unavailable",
                )
            if name == "type_text":
                typed = str(args.get("text") or "")
                token = str(args.get("element_token") or "")
                targeted = bool(token or args.get("element_index") is not None)
                profile = self.scenario.delivery_profile
                if profile == "focus_refused" and delivery_mode == "foreground":
                    return self._top_level_error(
                        "Exact foreground focus proof failed before input.",
                        "focus_refused",
                    )
                if targeted:
                    index = self.element_indexes.get(token)
                    if index is None and args.get("element_index") is not None:
                        index = int(args["element_index"])
                    if index is None:
                        return self._error("element token is stale", "stale_element")
                    self.element_values[index] = self.element_values.get(index, "") + typed
                    self.mutated_element_indexes.add(index)
                    self.document_value = self.element_values[index]
                    effect = (
                        "unverifiable"
                        if profile in {"web_targeted_unverifiable", "catalyst_value_unavailable"}
                        else "confirmed"
                        if profile
                        in {
                            "macos_native_ax_confirmed",
                            "windows_native_uia_confirmed",
                        }
                        else self.scenario.element_type_effect
                    )
                    return self._result({
                        "path": (
                            "key_events"
                            if profile == "web_targeted_unverifiable"
                            else "uia"
                            if profile == "windows_native_uia_confirmed"
                            else "ax"
                            if profile == "macos_native_ax_confirmed"
                            else "accessibility"
                        ),
                        "effect": effect,
                        "verified": effect == "confirmed",
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
            if name == "set_value":
                replacement = str(args.get("value") or "")
                token = str(args.get("element_token") or "")
                index = self.element_indexes.get(token)
                profile = self.scenario.delivery_profile
                if (
                    self.scenario.set_value_updates_document
                    or profile == "native_exact_set_value"
                ):
                    self.document_value = replacement
                    if index is not None:
                        self.element_values[index] = replacement
                        self.mutated_element_indexes.add(index)
                effect = (
                    "unverifiable"
                    if profile in {"web_targeted_unverifiable", "catalyst_value_unavailable"}
                    else "confirmed"
                    if profile
                    in {
                        "native_exact_set_value",
                        "macos_native_ax_confirmed",
                        "windows_native_uia_confirmed",
                    }
                    else self.scenario.element_type_effect
                )
                return self._result({
                    "path": (
                        "uia"
                        if profile == "windows_native_uia_confirmed"
                        else "ax"
                        if profile == "macos_native_ax_confirmed"
                        else "accessibility"
                    ),
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
                if label in self.scenario.close_target_after_labels:
                    self.closed_targets.add(
                        (int(args.get("pid") or 0), int(args.get("window_id") or 0))
                    )
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
                "route": self.scenario.action_route,
                "cause": self.scenario.action_cause,
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
