"""Narrow per-window native adapter; the legacy shared bridge is not authority.

The v1 HTTP handshake deliberately advertises native availability as false.
This implementation is ready for a trusted shell to compose after its separate
platform gate; importing it never opens windows, files or native libraries.
"""

from __future__ import annotations

import hmac
from ipaddress import ip_address
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

_REFERENCE = re.compile(r"[A-Za-z0-9:_-]{1,256}")
_OPERATIONS = frozenset({"discover", "select_file", "select_folder", "clipboard_read",
                         "clipboard_write", "open_external", "managed_window", "save"})


def _unavailable(reason: str = "unsupported") -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def safe_external_url(value: object) -> str | None:
    """Permit explicit HTTP(S) browser navigation without credentials or controls."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        _ = parsed.port
        return value
    except ValueError:
        return None


class NativeDriver(Protocol):
    """Host operations supplied by the trusted shell, never by browser payloads."""

    def select(self, kind: str) -> str | None: ...
    def clipboard_read(self) -> str | None: ...
    def clipboard_write(self, text: str) -> bool: ...
    def open_external(self, url: str) -> bool: ...
    def managed_window(self, route: str) -> bool: ...
    def save(self, reference: str, suggested_name: str, authorized: Callable[[], bool]) -> bool | None: ...
    def capabilities(self) -> list[str]: ...


class PyWebViewDriver:
    """Use one supplied window; file paths stay inside trusted Python callbacks."""

    def __init__(self, window: Any, *, open_window: Callable[[str], bool] | None = None,
                 read_clipboard: Callable[[], str | None] | None = None,
                 write_clipboard: Callable[[str], bool] | None = None,
                 save_reference: Callable[[str, Path], bool] | None = None,
                 open_external: Callable[[str], bool] | None = None) -> None:
        self._window = window
        self._open_window = open_window
        self._read_clipboard = read_clipboard
        self._write_clipboard = write_clipboard
        self._save_reference = save_reference
        self._open_external = open_external

    def capabilities(self) -> list[str]:
        result = ["select_file", "select_folder", "open_external"]
        for name, callback in (("managed_window", self._open_window), ("clipboard_read", self._read_clipboard),
                               ("clipboard_write", self._write_clipboard), ("save", self._save_reference)):
            if callback is not None:
                result.append(name)
        return result

    def select(self, kind: str) -> str | None:
        import webview
        selected = self._window.create_file_dialog(webview.FOLDER_DIALOG if kind == "folder" else webview.OPEN_DIALOG,
                                                   allow_multiple=False)
        return str(selected[0]) if selected else None

    def clipboard_read(self) -> str | None:
        return self._read_clipboard() if self._read_clipboard is not None else None

    def clipboard_write(self, text: str) -> bool:
        return bool(self._write_clipboard and self._write_clipboard(text))

    def open_external(self, url: str) -> bool:
        if self._open_external is not None:
            return self._open_external(url)
        import webbrowser
        return webbrowser.open(url)

    def managed_window(self, route: str) -> bool:
        return bool(self._open_window and self._open_window(route))

    def save(self, reference: str, suggested_name: str, authorized: Callable[[], bool]) -> bool | None:
        if self._save_reference is None:
            return False
        import webview
        selected = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=suggested_name)
        if not selected:
            return None
        if not authorized():
            return False
        return self._save_reference(reference, Path(selected[0]))


class NativeClientBridge:
    """One document lease with revocation and current-window checks at dispatch.

    Only ``native_client_dispatch`` is exposed to JavaScript. Underscored lease
    methods must be called by the shell's before_load/loaded/closed handlers.
    Selection registration is the existing backend's authority callback, which
    must revalidate resource/session/root permissions and return an opaque ref.
    """

    def __init__(self, *, instance_id: str, window_id: str, origin: str,
                 current_url: Callable[[], str | None], driver: NativeDriver,
                 register_selection: Callable[[str, Path], str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        parsed = urlsplit(origin)
        if not safe_external_url(origin) or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("invalid_native_origin")
        try:
            loopback = parsed.hostname == "localhost" or ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("native_origin_must_be_loopback")
        self._origin = origin.rstrip("/")
        self._instance = instance_id
        self._window = window_id
        self._current_url = current_url
        self._driver = driver
        self._register = register_selection
        self._clock = clock
        self._lock = threading.RLock()
        self._token = ""
        self._epoch = 0
        self._expires = 0.0

    def _at_shell(self) -> bool:
        try:
            url = self._current_url()
            if not isinstance(url, str) or not safe_external_url(url):
                return False
            parsed = urlsplit(url)
            return (f"{parsed.scheme}://{parsed.netloc}" == self._origin
                    and bool(re.fullmatch(r"/app-v2/(?:[A-Za-z0-9_-]+/?)*", parsed.path)))
        except Exception:
            return False

    def _invalidate(self, *_args: Any) -> None:
        with self._lock:
            self._token = ""
            self._epoch += 1

    def _bind_loaded_document(self) -> dict[str, Any] | None:
        with self._lock:
            self._invalidate()
            if not self._at_shell():
                return None
            self._token = secrets.token_urlsafe(32)
            self._expires = self._clock() + 1800
            return {"instanceId": self._instance, "windowId": self._window,
                    "epoch": self._epoch, "token": self._token}

    def _valid(self, proof: Mapping[str, Any]) -> bool:
        if not self._at_shell() or self._clock() >= self._expires:
            self._invalidate()
            return False
        return bool(self._token
                    and set(proof) == {"instanceId", "windowId", "epoch", "token"}
                    and proof.get("instanceId") == self._instance and proof.get("windowId") == self._window
                    and type(proof.get("epoch")) is int and proof["epoch"] == self._epoch
                    and isinstance(proof.get("token"), str)
                    and hmac.compare_digest(proof["token"], self._token))

    def native_client_dispatch(self, proof: object, operation: object, payload: object) -> dict[str, Any]:
        """Validate the lease and closed request before any native effect."""
        if not isinstance(proof, dict) or not isinstance(payload, dict) or not isinstance(operation, str):
            return _unavailable("invalid_request")
        try:
            if len(json.dumps(payload)) > 64 * 1024 or operation not in _OPERATIONS:
                return _unavailable("invalid_request")
            with self._lock:
                if not self._valid(proof):
                    return _unavailable("native_proof_required")
                epoch = self._epoch
                available = self._driver.capabilities()
                if not self._valid(proof) or epoch != self._epoch:
                    return _unavailable("native_proof_required")
                if self._register is None:
                    available = [name for name in available if name not in {"select_file", "select_folder"}]
                if operation == "discover" and not payload:
                    platform = {"win32": "windows", "darwin": "macos", "linux": "linux"}.get(sys.platform, "unknown")
                    return {"status": "ok", "value": {"kind": "pywebview", "platform": platform,
                            "capabilities": available, "instanceId": self._instance, "windowId": self._window,
                            "epoch": epoch}}
                if operation not in available:
                    return _unavailable()
            # A picker can stay open through navigation. Do not hold the lock:
            # navigation revokes immediately and the completion is checked again.
            if operation in {"select_file", "select_folder"} and not payload:
                kind = "folder" if operation == "select_folder" else "file"
                path = self._driver.select(kind)
                with self._lock:
                    if not self._valid(proof) or epoch != self._epoch:
                        return _unavailable("native_proof_required")
                    if not path:
                        return {"status": "cancelled"}
                    assert self._register is not None
                    reference = self._register(kind, Path(path))
                    if not self._valid(proof) or epoch != self._epoch:
                        return _unavailable("native_proof_required")
                    if not _REFERENCE.fullmatch(reference):
                        return _unavailable("invalid_reference")
                    return {"status": "ok", "value": {"reference": reference, "kind": kind}}
            if (operation == "save" and set(payload) == {"reference", "name"}
                    and isinstance(payload["reference"], str) and _REFERENCE.fullmatch(payload["reference"])
                    and isinstance(payload["name"], str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,119}", payload["name"])):
                def authorized() -> bool:
                    with self._lock:
                        return self._valid(proof)
                result = self._driver.save(payload["reference"], payload["name"], authorized)
                if not authorized():
                    return _unavailable("native_proof_required")
                return {"status": "cancelled"} if result is None else ({"status": "ok", "value": None} if result else _unavailable())
            with self._lock:
                if not self._valid(proof):
                    return _unavailable("native_proof_required")
                if operation == "clipboard_read" and not payload:
                    value = self._driver.clipboard_read()
                    if not self._valid(proof) or epoch != self._epoch:
                        return _unavailable("native_proof_required")
                    if value is None or len(value.encode("utf-8")) > 64 * 1024:
                        return _unavailable()
                    return {"status": "ok", "value": value}
                if operation == "clipboard_write" and set(payload) == {"text"} and isinstance(payload["text"], str):
                    result = self._driver.clipboard_write(payload["text"])
                elif operation == "open_external" and set(payload) == {"url"} and safe_external_url(payload["url"]):
                    result = self._driver.open_external(payload["url"])
                elif (operation == "managed_window" and set(payload) == {"route"}
                      and isinstance(payload["route"], str)
                      and re.fullmatch(r"/app-v2/(?:[A-Za-z0-9_-]+/?)*", payload["route"])):
                    result = self._driver.managed_window(payload["route"])
                else:
                    return _unavailable("invalid_request")
                if not self._valid(proof) or epoch != self._epoch:
                    return _unavailable("native_proof_required")
                return {"status": "cancelled"} if result is None else ({"status": "ok", "value": None} if result else _unavailable())
        except Exception:
            # Neither native exception details nor picker paths cross to JS.
            return _unavailable("operation_failed")


def attach_native_client(window: Any, *, instance_id: str, origin: str, driver: NativeDriver,
                         register_selection: Callable[[str, Path], str] | None = None) -> NativeClientBridge:
    """Attach only to a newly created trusted /app-v2 window, never legacy API.

    The caller must gate composition on independently negotiated native support.
    v1.0 advertises unavailable, so the production launcher does not call this.
    A managed-window callback must create another independently bound window.
    """
    bridge = NativeClientBridge(instance_id=instance_id, window_id=str(window.uid), origin=origin,
                                current_url=window.get_current_url, driver=driver,
                                register_selection=register_selection)

    def loaded(*_args: Any) -> None:
        proof = bridge._bind_loaded_document()
        if proof is not None:
            # Token is a closure value, never a storage item, URL or public flag.
            script = "(() => { if (window !== window.top) return; const proof = " + json.dumps(proof) + "; "
            script += "Object.defineProperty(window, '__ROW_BOT_NATIVE_CLIENT__', { configurable: true, "
            script += "value: { dispatch: (operation, payload) => window.pywebview.api.native_client_dispatch(proof, operation, payload) } }); })();"
            window.evaluate_js(script)

    window.events.before_load += bridge._invalidate
    window.events.closed += bridge._invalidate
    window.events.loaded += loaded
    window.expose(bridge.native_client_dispatch)
    return bridge
