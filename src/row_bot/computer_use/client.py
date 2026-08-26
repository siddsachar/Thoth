"""Private, allowlisted Cua Driver MCP adapter."""

from __future__ import annotations

import base64
import binascii
import io
import math
import os
import platform
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from row_bot.mcp_client.results import RawCallResult, raw_call_result
from row_bot.mcp_client.runtime import PrivateMcpSession

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1456
MAX_ELEMENTS = 2_000
MAX_TREE_DEPTH = 25
MAX_FIELD_CHARS = 512
MAX_SEMANTIC_TEXT = 2 * 1024 * 1024
MAX_GEOMETRY_ABS = 1_000_000.0
ALLOWED_IMAGE_MIME = frozenset({"image/png", "image/jpeg"})

MODEL_ACTION_TO_CUA = {
    "list_apps": "list_apps",
    "list_windows": "list_windows",
    "launch_app": "launch_app",
    "capture": "get_window_state",
    "focus": "bring_to_front",
    "click": "click",
    "double_click": "double_click",
    "right_click": "right_click",
    "type": "type_text",
    "replace_text": "set_value",
    "key": "press_key",
    "scroll": "scroll",
    "drag": "drag",
}

INTERNAL_TOOLS = frozenset({"set_config", "health_report", "check_permissions", "start_session", "end_session"})
ALLOWED_CUA_TOOLS = frozenset(MODEL_ACTION_TO_CUA.values()) | INTERNAL_TOOLS | {"hotkey"}
FORBIDDEN_TOOL_FAMILIES = frozenset({
    "page", "get_desktop_state", "start_recording", "stop_recording",
    "get_recording_state", "check_for_update", "install_ffmpeg", "kill_app",
    "set_agent_cursor", "zoom", "move_cursor", "get_config",
})


class CuaTransport(Protocol):
    def open(self) -> None: ...
    def call_raw(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class CuaElement:
    token: str
    index: int
    role: str
    label: str
    value: str = field(repr=False)
    bounds: tuple[float, float, float, float]
    depth: int
    parent_index: int | None = None
    visible: bool | None = None
    enabled: bool | None = None
    selected: bool | None = None
    checked: bool | None = None
    expanded: bool | None = None
    pressed: bool | None = None
    toggled: bool | None = None
    editable: bool | None = None
    read_only: bool | None = None
    value_available: bool = True
    in_web_content: bool = False


@dataclass(frozen=True)
class CuaLaunchProfile:
    argv: tuple[str, ...]
    permission_identity: str


def cua_launch_profile() -> CuaLaunchProfile:
    """Return the reviewed private MCP process profile for this host."""

    if platform.system().casefold() == "darwin":
        return CuaLaunchProfile(
            argv=("mcp", "--direct"),
            permission_identity="row_bot_host",
        )
    return CuaLaunchProfile(
        argv=("mcp",),
        permission_identity="interactive_windows_session",
    )


@dataclass(frozen=True)
class CuaResponse:
    text: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    image_bytes: bytes | None = None
    image_mime: str = ""
    image_width: int = 0
    image_height: int = 0
    elements: tuple[CuaElement, ...] = ()
    snapshot_id: str = ""
    backend_declared_count: int | None = None
    backend_filtered_count: int | None = None
    backend_received_count: int = 0
    backend_limited: bool | None = None
    backend_sparse: bool = False
    locally_filtered_count: int = 0
    local_limit_reasons: tuple[str, ...] = ()
    truncated: bool = False
    is_error: bool = False
    error_code: str = ""


_MUTATION_ENUM_FIELDS = {
    "effect": frozenset(
        {"changed", "confirmed", "partial", "unverifiable", "suspected_noop", "refused"}
    ),
    "delivery": frozenset({"background", "foreground", "not_applicable", "unknown"}),
    "delivery_mode": frozenset(
        {"background", "foreground", "not_applicable", "unknown"}
    ),
    "route": frozenset(
        {"ax", "uia", "accessibility", "synthetic_events", "global_input", "unknown"}
    ),
    "path": frozenset(
        {"ax", "uia", "accessibility", "key_events", "send_input", "unknown"}
    ),
    "status": frozenset({"satisfied", "unsatisfied", "unknown"}),
}
_MUTATION_ERROR_CODES = frozenset(
    {
        "background_unavailable",
        "driver_unavailable",
        "not_supported",
        "permission_denied",
        "stale_element",
        "temporarily_unavailable",
        "timeout",
        "unsupported",
        "unsupported_capability",
        "unsupported_role",
        "value_not_supported",
        "focus_refused",
        "foreground_required",
        "snapshot_expired",
    }
)


def _normalized_mutation_response(response: CuaResponse) -> CuaResponse:
    """Drop driver prose at the private boundary for content-bearing mutations."""

    structured: dict[str, Any] = {}
    if isinstance(response.structured.get("verified"), bool):
        structured["verified"] = response.structured["verified"]
    for key, allowed in _MUTATION_ENUM_FIELDS.items():
        value = str(response.structured.get(key) or "").strip().casefold().replace("-", "_")
        if value in allowed:
            structured[key] = value
    error_code = str(response.error_code or "").strip().casefold().replace("-", "_")
    error_code = error_code if error_code in _MUTATION_ERROR_CODES else (
        "driver_failed" if response.is_error else ""
    )
    if error_code:
        structured["error"] = {"code": error_code}
    return CuaResponse(
        text="",
        structured=structured,
        is_error=response.is_error,
        error_code=error_code,
    )


def build_cua_environment(session_id: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Return the deliberately small child environment for Cua."""

    source = dict(os.environ if environ is None else environ)
    common = {"PATH", "Path", "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL"}
    windows = {"SystemRoot", "WINDIR", "COMSPEC", "APPDATA", "LOCALAPPDATA", "SESSIONNAME"}
    desktop = {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    allowed = common | windows | desktop
    result = {key: str(value) for key, value in source.items() if key in allowed and value is not None}
    result["CUA_DRIVER_RS_UPDATE_CHECK"] = "0"
    result["ROW_BOT_CUA_SESSION_ID"] = str(session_id)
    result.pop("CUA_DRIVER_EMBEDDED", None)
    result.pop("CUA_DRIVER_PARENT_LIVENESS_STDIN", None)
    result.pop("CUA_DRIVER_HOST_BUNDLE_ID", None)
    result.pop("CUA_DRIVER_RS_TELEMETRY_ENABLED", None)
    result.pop("CUA_DRIVER_RS_TELEMETRY_DEBUG", None)
    return result


def _trim(value: Any) -> str:
    text = str(value or "")
    return text[:MAX_FIELD_CHARS] + ("…" if len(text) > MAX_FIELD_CHARS else "")


def _non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _bounded_geometry(frame: Any) -> tuple[float, float, float, float] | None:
    source = frame if isinstance(frame, dict) else {}
    try:
        values = (
            float(source.get("x") or 0),
            float(source.get("y") or 0),
            float(source.get("w") or source.get("width") or 0),
            float(source.get("h") or source.get("height") or 0),
        )
    except (TypeError, ValueError, OverflowError):
        return None
    x, y, width, height = values
    if (
        not all(math.isfinite(value) for value in values)
        or abs(x) > MAX_GEOMETRY_ABS
        or abs(y) > MAX_GEOMETRY_ABS
        or width < 0
        or height < 0
        or width > MAX_GEOMETRY_ABS
        or height > MAX_GEOMETRY_ABS
    ):
        return None
    return values


def _decode_image(data: str, mime: str) -> tuple[bytes, int, int]:
    if mime not in ALLOWED_IMAGE_MIME:
        raise ValueError(f"Unsupported Cua image MIME type: {mime or 'missing'}")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Cua returned malformed base64 image data") from exc
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("Cua screenshot exceeds the 8 MiB decoded limit")
    if mime == "image/png" and not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Cua screenshot MIME and PNG magic bytes do not agree")
    if mime == "image/jpeg" and not decoded.startswith(b"\xff\xd8\xff"):
        raise ValueError("Cua screenshot MIME and JPEG magic bytes do not agree")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(decoded)) as image:
            width, height = image.size
            actual = (image.format or "").upper()
            expected = "PNG" if mime == "image/png" else "JPEG"
            if actual != expected:
                raise ValueError("Cua screenshot MIME and decoded format do not agree")
    except ImportError:
        if mime == "image/png" and len(decoded) >= 24:
            width = int.from_bytes(decoded[16:20], "big")
            height = int.from_bytes(decoded[20:24], "big")
        else:
            raise ValueError("Pillow is required to validate JPEG Cua screenshots")
    if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_DIMENSION:
        raise ValueError("Cua screenshot dimensions exceed the 1456 pixel limit")
    return decoded, int(width), int(height)


def parse_cua_result(result: Any) -> CuaResponse:
    raw = result if isinstance(result, RawCallResult) else raw_call_result(result)
    texts: list[str] = []
    image_bytes: bytes | None = None
    image_mime = ""
    image_width = image_height = 0
    for block in raw.content:
        if block.kind == "image" or block.data:
            if image_bytes is not None:
                continue
            image_bytes, image_width, image_height = _decode_image(block.data, block.mime_type)
            image_mime = block.mime_type
        elif block.text:
            texts.append(_trim(block.text))
    structured = raw.structured_content if isinstance(raw.structured_content, dict) else {}
    elements_raw = structured.get("elements") if isinstance(structured.get("elements"), list) else []
    received_count = len(elements_raw)
    declared_count = _non_negative_int(
        structured.get("total_element_count", structured.get("element_count"))
    )
    filtered_count = _non_negative_int(
        structured.get(
            "returned_element_count",
            structured.get("filtered_element_count", received_count),
        )
    )
    elements: list[CuaElement] = []
    local_reasons: set[str] = set()
    if received_count > MAX_ELEMENTS:
        local_reasons.add("element_limit")
    semantic_bytes = 0
    indexes: set[int] = set()
    tokens: set[str] = set()
    depths: dict[int, int] = {}
    for item in elements_raw:
        if len(elements) >= MAX_ELEMENTS:
            local_reasons.add("element_limit")
            break
        if not isinstance(item, dict):
            local_reasons.add("invalid_element")
            continue
        index = _non_negative_int(item.get("element_index"))
        depth = _non_negative_int(item.get("depth"))
        token = _trim(item.get("element_token"))
        parent = _non_negative_int(item.get("parent_index"))
        if index is None or not token or index in indexes or token in tokens:
            local_reasons.add("invalid_identity")
            continue
        if depth is None or depth > MAX_TREE_DEPTH:
            local_reasons.add("depth_limit")
            continue
        if parent is not None and (parent not in indexes or depths[parent] >= depth):
            local_reasons.add("invalid_topology")
            continue
        bounds = _bounded_geometry(item.get("frame"))
        if bounds is None:
            local_reasons.add("invalid_geometry")
            continue
        element = CuaElement(
            token=token,
            index=index,
            role=_trim(item.get("role")),
            label=_trim(item.get("label")),
            value=_trim(item.get("value")),
            bounds=bounds,
            depth=depth,
            parent_index=parent,
            visible=(bool(item["visible"]) if "visible" in item else None),
            enabled=(bool(item["enabled"]) if "enabled" in item else None),
            selected=(bool(item["selected"]) if "selected" in item else None),
            checked=(bool(item["checked"]) if "checked" in item else None),
            expanded=(bool(item["expanded"]) if "expanded" in item else None),
            pressed=(bool(item["pressed"]) if "pressed" in item else None),
            toggled=(bool(item["toggled"]) if "toggled" in item else None),
            editable=(bool(item["editable"]) if "editable" in item else None),
            read_only=(
                bool(item.get("read_only", item.get("readonly")))
                if "read_only" in item or "readonly" in item
                else None
            ),
            value_available="value" in item and item.get("value") is not None,
            in_web_content=bool(item.get("in_web_content")),
        )
        element_bytes = sum(
            len(value.encode("utf-8"))
            for value in (element.token, element.role, element.label, element.value)
        )
        if semantic_bytes + element_bytes > MAX_SEMANTIC_TEXT:
            local_reasons.add("byte_limit")
            break
        semantic_bytes += element_bytes
        indexes.add(index)
        tokens.add(token)
        depths[index] = depth
        elements.append(element)
    explicit_limited = structured.get("truncated")
    if isinstance(explicit_limited, bool):
        backend_limited: bool | None = explicit_limited
    elif declared_count is not None and declared_count >= MAX_ELEMENTS:
        backend_limited = True
    elif structured.get("elements_complete") is True:
        backend_limited = False
    else:
        backend_limited = None
    backend_sparse = bool(
        structured.get("degraded")
        or (declared_count is not None and declared_count > 0 and received_count == 0)
    )
    error = structured.get("error") if isinstance(structured.get("error"), dict) else {}
    error_code = str(
        error.get("code")
        or structured.get("error_code")
        or (structured.get("code") if raw.is_error else "")
        or ""
    )
    return CuaResponse(
        text="\n".join(texts)[:MAX_SEMANTIC_TEXT],
        structured=dict(structured),
        image_bytes=image_bytes,
        image_mime=image_mime,
        image_width=image_width,
        image_height=image_height,
        elements=tuple(elements),
        snapshot_id=_trim(structured.get("snapshot_id")),
        backend_declared_count=declared_count,
        backend_filtered_count=filtered_count,
        backend_received_count=received_count,
        backend_limited=backend_limited,
        backend_sparse=backend_sparse,
        locally_filtered_count=max(0, received_count - len(elements)),
        local_limit_reasons=tuple(sorted(local_reasons)),
        truncated=backend_limited is True or bool(local_reasons),
        is_error=bool(raw.is_error),
        error_code=error_code,
    )


class CuaClient:
    """One private Cua MCP connection with a hard tool allowlist."""

    def __init__(
        self,
        executable: str | Path,
        *,
        session_id: str | None = None,
        transport_factory: Callable[[str, str, dict[str, str]], CuaTransport] | None = None,
        contract_version: str = "0.20.0",
        capabilities: frozenset[str] | None = None,
    ) -> None:
        self.executable = str(Path(executable))
        self.session_id = session_id or f"row-bot-{uuid.uuid4().hex}"
        self._transport_factory = transport_factory or self._default_transport
        self.launch_profile = cua_launch_profile()
        self.contract_version = str(contract_version)
        self.capabilities = frozenset(capabilities or ())
        self._transport: CuaTransport | None = None
        self.connection_generation = 0

    def _default_transport(
        self,
        executable: str,
        _session_id: str,
        env: dict[str, str],
    ) -> CuaTransport:
        return PrivateMcpSession(
            command=executable,
            args=list(self.launch_profile.argv),
            env=env,
            timeout=120.0,
        )

    def start(self) -> None:
        if self._transport is not None:
            return
        from row_bot.computer_use.readiness import require_cua_disclosure

        require_cua_disclosure()
        transport = self._transport_factory(
            self.executable,
            self.session_id,
            build_cua_environment(self.session_id),
        )
        transport.open()
        self._transport = transport
        self.connection_generation += 1
        try:
            self.call_internal("start_session", {"session": self.session_id})
        except BaseException:
            self.close()
            raise

    def close(self, *, graceful: bool = True) -> None:
        transport = self._transport
        self._transport = None
        if transport is None:
            return
        if graceful:
            try:
                transport.call_raw("end_session", {"session": self.session_id})
            except Exception:
                pass
        transport.close()

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> CuaResponse:
        capability_tool = (
            tool_name in {"verify_state", "invoke_menu"}
            and tool_name in self.capabilities
        )
        if (
            (tool_name not in ALLOWED_CUA_TOOLS and not capability_tool)
            or tool_name in FORBIDDEN_TOOL_FAMILIES
        ):
            raise PermissionError(f"Cua tool is not allowlisted: {tool_name}")
        if self._transport is None:
            self.start()
        assert self._transport is not None
        response = parse_cua_result(self._transport.call_raw(tool_name, arguments))
        if tool_name in {"type_text", "set_value"}:
            return _normalized_mutation_response(response)
        return response

    def call_internal(self, tool_name: str, arguments: dict[str, Any] | None = None) -> CuaResponse:
        if tool_name not in INTERNAL_TOOLS:
            raise PermissionError(f"Cua internal tool is not allowlisted: {tool_name}")
        if tool_name == "set_config":
            raise PermissionError("Cua configuration mutation is not approved")
        return self._call(tool_name, dict(arguments or {}))

    def call_action(self, action: str, arguments: dict[str, Any] | None = None) -> CuaResponse:
        tool_name = MODEL_ACTION_TO_CUA.get(str(action))
        if not tool_name:
            raise ValueError(f"Unsupported Computer action: {action}")
        safe = dict(arguments or {})
        safe.setdefault("session", self.session_id)
        return self._call(tool_name, safe)

    def call_reviewed_driver_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> CuaResponse:
        """Service-only access to a reviewed input tool not in the model schema."""

        approved = set(MODEL_ACTION_TO_CUA.values()) | {"hotkey"}
        if tool_name in {"verify_state", "invoke_menu"} and tool_name in self.capabilities:
            approved.add(tool_name)
        if tool_name not in approved:
            raise PermissionError(f"Cua driver tool is not approved for Computer actions: {tool_name}")
        safe = dict(arguments or {})
        safe.setdefault("session", self.session_id)
        return self._call(tool_name, safe)

    def supports_capability(self, name: str) -> bool:
        return str(name) in self.capabilities
