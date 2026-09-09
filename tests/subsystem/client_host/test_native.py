from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from row_bot.native_client import NativeClientBridge, PyWebViewDriver, attach_native_client, safe_external_url


class Driver:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_select: Callable[[], None] = lambda: None
        self.on_save: Callable[[], None] = lambda: None
        self.selected: str | None = "/synthetic/selected.txt"

    def capabilities(self) -> list[str]:
        return ["select_file", "select_folder", "clipboard_read", "clipboard_write", "open_external", "managed_window", "save"]

    def select(self, kind: str) -> str | None:
        self.calls.append("select:" + kind)
        self.on_select()
        return self.selected

    def clipboard_read(self) -> str:
        self.calls.append("clipboard_read")
        return "fixture clipboard"

    def clipboard_write(self, text: str) -> bool:
        self.calls.append("clipboard_write:" + text)
        return True

    def open_external(self, url: str) -> bool:
        self.calls.append("open_external:" + url)
        return True

    def managed_window(self, route: str) -> bool:
        self.calls.append("managed_window:" + route)
        return True

    def save(self, reference: str, suggested_name: str, authorized: Callable[[], bool]) -> bool:
        self.on_save()
        if not authorized():
            return False
        self.calls.append("save:" + reference)
        return True


@pytest.fixture
def native():
    driver = Driver()
    state = {"url": "http://localhost:8080/app-v2/", "clock": 100.0}
    registered: list[tuple[str, Path]] = []
    def register(kind: str, path: Path) -> str:
        registered.append((kind, path))
        return "fixture_reference"
    bridge = NativeClientBridge(instance_id="instance", window_id="window", origin="http://localhost:8080",
                                current_url=lambda: state["url"], driver=driver, register_selection=register,
                                clock=lambda: state["clock"])
    proof = bridge._bind_loaded_document()
    assert proof
    return bridge, proof, driver, state, registered


def test_native_selection_registers_backend_ref_and_never_returns_path(native) -> None:
    bridge, proof, driver, _, registered = native
    result = bridge.native_client_dispatch(proof, "select_file", {})
    assert result == {"status": "ok", "value": {"reference": "fixture_reference", "kind": "file"}}
    assert registered == [("file", Path("/synthetic/selected.txt"))]
    assert driver.calls == ["select:file"]
    driver.selected = None
    assert bridge.native_client_dispatch(proof, "select_folder", {}) == {"status": "cancelled"}


@pytest.mark.parametrize("field,value", [("instanceId", "foreign"), ("windowId", "foreign"), ("epoch", 90), ("token", "fake")])
def test_spoofed_identity_cannot_invoke_driver(native, field: str, value: object) -> None:
    bridge, proof, driver, _, _ = native
    proof[field] = value
    assert bridge.native_client_dispatch(proof, "select_file", {})["status"] == "unavailable"
    assert driver.calls == []


@pytest.mark.parametrize("change", ["foreign", "legacy", "reload", "close", "expiry"])
def test_navigation_reload_close_and_expiry_invalidate_proof(native, change: str) -> None:
    bridge, proof, driver, state, _ = native
    if change in {"foreign", "legacy"}:
        state["url"] = "https://foreign.invalid/app-v2/" if change == "foreign" else "http://localhost:8080/"
    elif change == "reload":
        assert bridge._bind_loaded_document() != proof
    elif change == "close":
        bridge._invalidate()
    else:
        state["clock"] += 1800
    assert bridge.native_client_dispatch(proof, "clipboard_read", {})["status"] == "unavailable"
    assert driver.calls == []


def test_late_picker_and_save_do_not_register_or_write_after_navigation(native) -> None:
    bridge, proof, driver, _, registered = native
    driver.on_select = bridge._invalidate
    assert bridge.native_client_dispatch(proof, "select_file", {})["status"] == "unavailable"
    assert not registered
    proof = bridge._bind_loaded_document()
    driver.on_save = bridge._invalidate
    assert bridge.native_client_dispatch(proof, "save", {"reference": "fixture", "name": "fixture.txt"})["status"] == "unavailable"
    assert driver.calls == ["select:file"]


@pytest.mark.parametrize("operation", ["select_file", "clipboard_read", "clipboard_write", "discover"])
def test_reentrant_trusted_callback_cannot_return_data_after_revocation(native, monkeypatch: pytest.MonkeyPatch,
                                                                     operation: str) -> None:
    bridge, proof, driver, _, _ = native
    payload = {}
    def revoked(value):
        bridge._invalidate()
        return value
    if operation == "select_file":
        monkeypatch.setattr(bridge, "_register", lambda _kind, _path: revoked("fixture_reference"))
    elif operation == "clipboard_read":
        monkeypatch.setattr(driver, "clipboard_read", lambda: revoked("private fixture sentinel"))
    elif operation == "clipboard_write":
        monkeypatch.setattr(driver, "clipboard_write", lambda _text: revoked(True))
        payload = {"text": "fixture"}
    else:
        monkeypatch.setattr(driver, "capabilities", lambda: revoked(["discover"]))
    assert bridge.native_client_dispatch(proof, operation, payload) == {
        "status": "unavailable", "reason": "native_proof_required"}


@pytest.mark.parametrize("operation,payload", [
    ("open_external", {"url": "javascript:alert(1)"}), ("open_external", {"url": "https://user:secret@fixture.invalid/"}),
    ("managed_window", {"route": "https://foreign.invalid/"}), ("managed_window", {"route": "/app-v2/../api/launcher-shutdown"}),
    ("select_file", {"initial_dir": "/private"}), ("save", {"reference": "/private/file", "name": "a.txt"}),
    ("save", {"reference": "fixture", "name": "../file"}), ("clipboard_write", {"text": "x" * 65537}),
    ("shell", {"command": "anything"}),
])
def test_invalid_or_arbitrary_native_payloads_have_zero_effect(native, operation: str, payload: dict) -> None:
    bridge, proof, driver, _, registered = native
    assert bridge.native_client_dispatch(proof, operation, payload)["status"] == "unavailable"
    assert driver.calls == [] and registered == []


def test_all_narrow_operations_and_platform_discovery(native) -> None:
    bridge, proof, driver, _, _ = native
    discovery = bridge.native_client_dispatch(proof, "discover", {})
    assert discovery["value"]["kind"] == "pywebview"
    assert "token" not in discovery["value"]
    for operation, payload in [("clipboard_read", {}), ("clipboard_write", {"text": "fixture"}),
                               ("open_external", {"url": "https://example.invalid/help"}),
                               ("managed_window", {"route": "/app-v2/"}),
                               ("save", {"reference": "fixture_reference", "name": "fixture.txt"})]:
        assert bridge.native_client_dispatch(proof, operation, payload)["status"] == "ok"
    assert len(driver.calls) == 5


def test_no_backend_registrar_means_no_native_picker(native) -> None:
    _, _, driver, _, _ = native
    bridge = NativeClientBridge(instance_id="i", window_id="w", origin="http://localhost:8080",
                                current_url=lambda: "http://localhost:8080/app-v2/", driver=driver)
    proof = bridge._bind_loaded_document()
    assert bridge.native_client_dispatch(proof, "select_file", {})["status"] == "unavailable"
    assert driver.calls == []


def test_native_exception_never_exposes_private_paths(native) -> None:
    bridge, proof, driver, _, _ = native
    def fail() -> None:
        raise RuntimeError("private path and secret sentinel")
    driver.on_select = fail
    assert bridge.native_client_dispatch(proof, "select_file", {}) == {"status": "unavailable", "reason": "operation_failed"}


def test_pywebview_driver_uses_exact_supplied_window_and_backend_save_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG="file", FOLDER_DIALOG="folder", SAVE_DIALOG="save"))
    calls = []
    window = SimpleNamespace(create_file_dialog=lambda kind, **kwargs: calls.append((kind, kwargs)) or ["/synthetic/file"])
    saved = []
    driver = PyWebViewDriver(window, save_reference=lambda reference, path: saved.append((reference, path)) or True,
                             open_external=lambda url: calls.append(url) or True)
    assert driver.select("file") == "/synthetic/file"
    assert driver.select("folder") == "/synthetic/file"
    assert not driver.save("fixture", "fixture.txt", lambda: False)
    assert saved == []
    assert driver.save("fixture", "fixture.txt", lambda: True)
    assert saved == [("fixture", Path("/synthetic/file"))]
    assert driver.clipboard_read() is None
    assert not driver.managed_window("/app-v2/")


def test_trusted_attach_installs_document_scoped_hook_and_revokes_on_events() -> None:
    class Event:
        def __init__(self):
            self.handlers = []
        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self
        def fire(self):
            for handler in self.handlers:
                handler()
    scripts = []
    exposed = []
    window = SimpleNamespace(uid="window", get_current_url=lambda: "http://localhost:8080/app-v2/",
                             events=SimpleNamespace(before_load=Event(), closed=Event(), loaded=Event()),
                             expose=lambda callback: exposed.append(callback), evaluate_js=scripts.append)
    bridge = attach_native_client(window, instance_id="i", origin="http://localhost:8080", driver=Driver())
    window.events.loaded.fire()
    assert len(exposed) == 1 and exposed[0].__name__ == "native_client_dispatch"
    assert "__ROW_BOT_NATIVE_CLIENT__" in scripts[0] and "localStorage" not in scripts[0]
    assert bridge._token
    window.events.before_load.fire()
    assert not bridge._token


@pytest.mark.parametrize("value", ["file:///secret", "//example.invalid", "https://example.invalid:bad", "https://example.invalid/\n", "https://a\\b", "data:text/html,test"])
def test_external_url_schemes_and_malformed_values(value: str) -> None:
    assert safe_external_url(value) is None


def test_trusted_shell_composition_cannot_bind_remote_content() -> None:
    with pytest.raises(ValueError, match="native_origin_must_be_loopback"):
        NativeClientBridge(instance_id="i", window_id="w", origin="https://remote.invalid",
                           current_url=lambda: "https://remote.invalid/app-v2/", driver=Driver())


def test_foreign_navigation_rejection_cannot_restore_old_proof_by_returning(native) -> None:
    bridge, proof, driver, state, _ = native
    state["url"] = "https://remote.invalid/"
    assert bridge.native_client_dispatch(proof, "clipboard_read", {})["status"] == "unavailable"
    state["url"] = "http://localhost:8080/app-v2/"
    assert bridge.native_client_dispatch(proof, "clipboard_read", {})["status"] == "unavailable"
    assert driver.calls == []
