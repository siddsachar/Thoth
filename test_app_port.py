from pathlib import Path

import app_port
import launcher


def test_get_app_port_defaults_and_validates_env():
    assert app_port.get_app_port(environ={}) == 8080
    assert app_port.get_app_port(environ={"THOTH_PORT": "8123"}) == 8123
    assert app_port.get_app_port(environ={"THOTH_PORT": "0"}) == 8080
    assert app_port.get_app_port(environ={"THOTH_PORT": "70000"}) == 8080
    assert app_port.get_app_port(environ={"THOTH_PORT": "not-a-port"}) == 8080


def test_launcher_selects_default_port_when_free(monkeypatch):
    checked_ports = []

    def fake_port_in_use(port):
        checked_ports.append(port)
        return False

    def fake_thoth_server(port):
        raise AssertionError(f"should not probe Thoth identity when preferred port is free: {port}")

    monkeypatch.setattr(launcher, "_is_port_in_use", fake_port_in_use)
    monkeypatch.setattr(launcher, "_is_thoth_server", fake_thoth_server)

    assert launcher._select_app_port(preferred=8080, max_tries=3) == (8080, False)
    assert checked_ports == [8080]


def test_launcher_reuses_existing_thoth_on_default_port(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port == 8080)
    monkeypatch.setattr(launcher, "_is_thoth_server", lambda port: port == 8080)

    assert launcher._select_app_port(preferred=8080, max_tries=3) == (8080, True)


def test_launcher_reuses_existing_thoth_on_dynamic_port(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port in {8080, 8081})
    monkeypatch.setattr(launcher, "_is_thoth_server", lambda port: port == 8081)

    assert launcher._select_app_port(preferred=8080, max_tries=4) == (8081, True)


def test_launcher_skips_foreign_ports_and_picks_next_free(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port in {8080, 8081})
    monkeypatch.setattr(launcher, "_is_thoth_server", lambda port: False)

    assert launcher._select_app_port(preferred=8080, max_tries=4) == (8082, False)


def test_launcher_reuses_existing_thoth_before_next_free(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port in {8080, 8081})
    monkeypatch.setattr(launcher, "_is_thoth_server", lambda port: port == 8081)

    assert launcher._select_app_port(preferred=8080, max_tries=4) == (8081, True)


def test_thoth_process_passes_selected_port_to_app(monkeypatch, tmp_path):
    captured = {}

    class _FakePopen:
        pid = 4242

        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _FakePopen()

    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda cls: tmp_path))

    process = launcher._ThothProcess(port=8125, host="127.0.0.1")
    process.start()

    assert captured["env"][app_port.THOTH_PORT_ENV] == "8125"
    assert captured["env"][app_port.THOTH_HOST_ENV] == "127.0.0.1"
    assert captured["env"]["THOTH_NATIVE"] == "1"
    assert captured["cmd"][-1].endswith("app.py")


def test_launcher_display_detection_on_headless_linux(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert launcher._has_display_server() is False

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert launcher._has_display_server() is True


def test_designer_publish_uses_active_app_port(monkeypatch):
    import designer.publish as publish

    calls = []

    class _FakeTunnelManager:
        def get_url(self, port):
            calls.append(("get_url", port))
            return None

        def is_available(self):
            return False

    monkeypatch.setenv(app_port.THOTH_PORT_ENV, "8126")
    monkeypatch.setattr(publish, "tunnel_manager", _FakeTunnelManager())

    base_url, is_public = publish.resolve_publish_base_url(ensure_public=True)

    assert calls == [("get_url", 8126)]
    assert base_url == "http://127.0.0.1:8126"
    assert is_public is False


def test_port_consumers_no_longer_lookup_main_tunnel_on_literal_8080():
    sms_source = Path("channels/sms.py").read_text(encoding="utf-8")
    settings_source = Path("ui/settings.py").read_text(encoding="utf-8")
    app_source = Path("app.py").read_text(encoding="utf-8")

    assert "tunnel_manager.get_url(get_app_port())" in sms_source
    assert "get_url(8080)" not in sms_source
    assert "app_port = get_app_port()" in settings_source
    assert "/api/launcher-ping" in app_source
    assert '"port": _APP_PORT' in app_source
