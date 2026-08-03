from pathlib import Path
import json
from types import SimpleNamespace

import row_bot.app_port as app_port
import row_bot.launcher as launcher


_LEGACY_PORT_ENV = "THOTH_PORT"
_LEGACY_HOST_ENV = "THOTH_HOST"
_LEGACY_DATA_ENV = "THOTH_DATA_DIR"
_LEGACY_NATIVE_ENV = "THOTH_NATIVE"


def test_get_app_port_defaults_and_validates_env():
    assert app_port.get_app_port(environ={}) == 8080
    assert app_port.get_app_port(environ={"ROW_BOT_PORT": "8123"}) == 8123
    assert app_port.get_app_port(environ={"ROW_BOT_PORT": "0"}) == 8080
    assert app_port.get_app_port(environ={"ROW_BOT_PORT": "70000"}) == 8080
    assert app_port.get_app_port(environ={"ROW_BOT_PORT": "not-a-port"}) == 8080
    assert app_port.get_app_port(environ={_LEGACY_PORT_ENV: "8123"}) == 8080


def test_get_app_host_defaults_and_preserves_explicit_values():
    assert app_port.DEFAULT_APP_HOST == "127.0.0.1"
    assert app_port.parse_app_host(None) == "127.0.0.1"
    assert app_port.parse_app_host("") == "127.0.0.1"
    assert app_port.parse_app_host("   ") == "127.0.0.1"
    assert app_port.parse_app_host(" 127.0.0.1 ") == "127.0.0.1"
    assert app_port.parse_app_host("0.0.0.0") == "0.0.0.0"
    assert app_port.parse_app_host("::") == "::"
    assert app_port.parse_app_host("192.168.1.20") == "192.168.1.20"
    assert app_port.parse_app_host("row-bot.local") == "row-bot.local"


def test_get_app_host_uses_only_current_environment_name():
    assert app_port.get_app_host(environ={}) == "127.0.0.1"
    assert app_port.get_app_host(environ={app_port.ROW_BOT_HOST_ENV: " 0.0.0.0 "}) == "0.0.0.0"
    assert app_port.get_app_host(environ={_LEGACY_HOST_ENV: "0.0.0.0"}) == "127.0.0.1"


def test_app_always_passes_shared_effective_host_to_nicegui():
    source = Path("src/row_bot/app.py").read_text(encoding="utf-8")

    assert "_APP_HOST = get_app_host()" in source
    assert '"host": _APP_HOST' in source
    assert 'if _APP_HOST:' not in source
    assert '_app_boot_event("startup_shell_ready", host=_APP_HOST, port=_APP_PORT)' in source


def test_launcher_host_precedence_is_cli_then_environment_then_loopback(monkeypatch):
    monkeypatch.delenv(app_port.ROW_BOT_HOST_ENV, raising=False)
    assert launcher._resolve_launch_host(None) == "127.0.0.1"

    monkeypatch.setenv(app_port.ROW_BOT_HOST_ENV, "0.0.0.0")
    assert launcher._resolve_launch_host(None) == "0.0.0.0"
    assert launcher._resolve_launch_host(" 192.168.1.20 ") == "192.168.1.20"


def test_launcher_local_url_and_browser_helper_use_explicit_loopback(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    assert launcher._url_for_port(8123) == "http://127.0.0.1:8123"
    launcher._open_in_browser(8123)

    assert opened == ["http://127.0.0.1:8123"]


def test_launcher_native_window_helper_uses_explicit_loopback(monkeypatch):
    captured = {}

    class _FakePopen:
        pid = 4242

        def poll(self):
            return None

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return _FakePopen()

    monkeypatch.setattr(launcher, "_has_display_server", lambda: True)
    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    process = launcher._open_window(8124)

    assert process is not None
    assert "http://127.0.0.1:8124" in captured["args"]


def test_launcher_selects_default_port_when_free(monkeypatch):
    checked_ports = []

    def fake_port_in_use(port):
        checked_ports.append(port)
        return False

    def fake_row_bot_server(port):
        raise AssertionError(f"should not probe app identity when preferred port is free: {port}")

    monkeypatch.setattr(launcher, "_is_port_in_use", fake_port_in_use)
    monkeypatch.setattr(launcher, "_is_row_bot_server", fake_row_bot_server)

    assert launcher._select_app_port(preferred=8080, max_tries=3) == (8080, False)
    assert checked_ports == [8080]


def test_launcher_reuses_existing_row_bot_on_default_port(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port == 8080)
    monkeypatch.setattr(launcher, "_is_row_bot_server", lambda port: port == 8080)

    assert launcher._select_app_port(preferred=8080, max_tries=3) == (8080, True)


def test_launcher_reuses_existing_row_bot_on_dynamic_port(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port in {8080, 8081})
    monkeypatch.setattr(launcher, "_is_row_bot_server", lambda port: port == 8081)

    assert launcher._select_app_port(preferred=8080, max_tries=4) == (8081, True)


def test_run_direct_reuses_existing_server_without_child_exit_check(monkeypatch):
    captured = {}
    args = SimpleNamespace(
        no_ollama=True,
        port=8080,
        host=None,
        no_splash=True,
        server=True,
        no_open=True,
        native=False,
    )

    monkeypatch.setattr(launcher, "_select_app_port", lambda preferred: (preferred, True))
    monkeypatch.setattr(launcher, "_wait_for_server", lambda port, server=None: captured.setdefault("server", server) is None)

    launcher._run_direct(args)

    assert captured["server"] is None


def test_launcher_skips_ollama_autostart_for_provider_model(monkeypatch, tmp_path):
    (tmp_path / "model_settings.json").write_text(
        json.dumps({"model": "model:codex:gpt-5.5"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROW_BOT_AUTO_START_OLLAMA", raising=False)
    monkeypatch.setattr(
        launcher,
        "_start_ollama",
        lambda: (_ for _ in ()).throw(AssertionError("should not start Ollama")),
    )

    assert launcher._should_auto_start_ollama() is False
    launcher._maybe_start_ollama()


def test_launcher_starts_ollama_for_saved_local_model(monkeypatch, tmp_path):
    (tmp_path / "model_settings.json").write_text(
        json.dumps({"model": "model:ollama:qwen3:14b"}),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROW_BOT_AUTO_START_OLLAMA", raising=False)
    monkeypatch.setattr(launcher, "_start_ollama", lambda: calls.append("start"))

    assert launcher._should_auto_start_ollama() is True
    launcher._maybe_start_ollama()
    assert calls == ["start"]


def test_launcher_starts_ollama_for_legacy_bare_local_model(monkeypatch, tmp_path):
    (tmp_path / "model_settings.json").write_text(
        json.dumps({"model": "huihui_ai/deepseek-r1-abliterated:14b"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROW_BOT_AUTO_START_OLLAMA", raising=False)

    assert launcher._should_auto_start_ollama() is True


def test_launcher_vision_setting_can_request_ollama(monkeypatch, tmp_path):
    (tmp_path / "model_settings.json").write_text(
        json.dumps({"model": "model:openai:gpt-5.5"}),
        encoding="utf-8",
    )
    (tmp_path / "vision_settings.json").write_text(
        json.dumps({"model": "gemma3:4b"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROW_BOT_AUTO_START_OLLAMA", raising=False)

    assert launcher._should_auto_start_ollama() is True


def test_launcher_no_ollama_forces_skip(monkeypatch, tmp_path):
    (tmp_path / "model_settings.json").write_text(
        json.dumps({"model": "model:ollama:qwen3:14b"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        launcher,
        "_start_ollama",
        lambda: (_ for _ in ()).throw(AssertionError("should not start Ollama")),
    )

    launcher._maybe_start_ollama(no_ollama=True)


def test_launcher_skips_foreign_ports_and_picks_next_free(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port in {8080, 8081})
    monkeypatch.setattr(launcher, "_is_row_bot_server", lambda port: False)

    assert launcher._select_app_port(preferred=8080, max_tries=4) == (8082, False)


def test_launcher_reuses_existing_row_bot_before_next_free(monkeypatch):
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda port: port in {8080, 8081})
    monkeypatch.setattr(launcher, "_is_row_bot_server", lambda port: port == 8081)

    assert launcher._select_app_port(preferred=8080, max_tries=4) == (8081, True)


def test_row_bot_process_passes_selected_port_to_app(monkeypatch, tmp_path):
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
    monkeypatch.setenv(_LEGACY_DATA_ENV, str(tmp_path / ".thoth"))
    monkeypatch.setenv(_LEGACY_PORT_ENV, "9000")
    monkeypatch.setenv(_LEGACY_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(_LEGACY_NATIVE_ENV, "1")

    process = launcher._RowBotProcess(port=8125, host="127.0.0.1")
    process.start()

    assert captured["env"][app_port.ROW_BOT_PORT_ENV] == "8125"
    assert captured["env"][app_port.ROW_BOT_HOST_ENV] == "127.0.0.1"
    assert _LEGACY_DATA_ENV not in captured["env"]
    assert _LEGACY_PORT_ENV not in captured["env"]
    assert _LEGACY_HOST_ENV not in captured["env"]
    assert captured["env"]["ROW_BOT_NATIVE"] == "1"
    assert _LEGACY_NATIVE_ENV not in captured["env"]
    assert captured["cmd"][-1].endswith("app.py")


def test_row_bot_process_passes_default_host_to_child(monkeypatch, tmp_path):
    captured = {}

    class _FakePopen:
        pid = 4242

        def poll(self):
            return None

    def _fake_popen(_cmd, **kwargs):
        captured.update(kwargs)
        return _FakePopen()

    monkeypatch.delenv(app_port.ROW_BOT_HOST_ENV, raising=False)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)

    process = launcher._RowBotProcess(port=8125)
    process.start()
    process._close_log_handle()

    assert process.host == "127.0.0.1"
    assert captured["env"][app_port.ROW_BOT_HOST_ENV] == "127.0.0.1"


def test_row_bot_process_preserves_environment_remote_host(monkeypatch, tmp_path):
    captured = {}

    class _FakePopen:
        pid = 4242

        def poll(self):
            return None

    def _fake_popen(_cmd, **kwargs):
        captured.update(kwargs)
        return _FakePopen()

    monkeypatch.setenv(app_port.ROW_BOT_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)

    process = launcher._RowBotProcess(port=8125)
    process.start()
    process._close_log_handle()

    assert process.host == "0.0.0.0"
    assert captured["env"][app_port.ROW_BOT_HOST_ENV] == "0.0.0.0"


def test_row_bot_process_stop_closes_parent_log_handle(monkeypatch, tmp_path):
    captured = {}

    class _FakePopen:
        pid = 4242

        def __init__(self):
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):  # noqa: ARG002
            self._alive = False
            return 0

        def kill(self):
            self._alive = False

    def _fake_popen(cmd, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return _FakePopen()

    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        launcher._RowBotProcess,
        "_request_graceful_shutdown",
        lambda self: False,
    )

    process = launcher._RowBotProcess(port=8125, host="127.0.0.1")
    process.start()
    log_handle = captured["stdout"]

    assert not log_handle.closed

    process.stop()

    assert process._log_handle is None
    assert log_handle.closed


def test_launcher_shutdown_source_contracts_are_wired():
    src = Path("src/row_bot/launcher.py").read_text(encoding="utf-8")

    assert "/api/launcher-shutdown" in src
    assert "def _close_log_handle" in src
    assert "self._close_log_handle()" in src
    assert '["taskkill", "/PID", str(proc.pid), "/T", "/F"]' in src
    assert "_GRACEFUL_SHUTDOWN_REQUEST_TIMEOUT = 3.0" in src
    assert "_GRACEFUL_SHUTDOWN_EXIT_TIMEOUT = 30.0" in src
    assert "_QUIT_WATCHDOG_TIMEOUT = 75.0" in src
    assert "graceful shutdown completed in" in src
    assert "mark_shutdown(reason)" in Path("src/row_bot/app.py").read_text(encoding="utf-8")


def test_launcher_display_detection_on_headless_linux(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert launcher._has_display_server() is False

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert launcher._has_display_server() is True


def test_designer_publish_uses_active_app_port(monkeypatch):
    import row_bot.designer.publish as publish

    calls = []

    class _FakeTunnelManager:
        def get_url(self, port):
            calls.append(("get_url", port))
            return None

        def is_available(self):
            return False

    monkeypatch.setenv(app_port.ROW_BOT_PORT_ENV, "8126")
    monkeypatch.setattr(publish, "tunnel_manager", _FakeTunnelManager())

    base_url, is_public = publish.resolve_publish_base_url(ensure_public=True)

    assert calls == [("get_url", 8126)]
    assert base_url == "http://127.0.0.1:8126"
    assert is_public is False


def test_port_consumers_no_longer_lookup_main_tunnel_on_literal_8080():
    sms_source = Path("src/row_bot/channels/sms.py").read_text(encoding="utf-8")
    settings_source = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")
    app_source = Path("src/row_bot/app.py").read_text(encoding="utf-8")

    assert "app_port = get_app_port()" in sms_source
    assert "tunnel_manager.get_url(app_port)" in sms_source
    assert "tunnel_manager.start_tunnel(app_port, label=\"sms\")" in sms_source
    assert "get_url(8080)" not in sms_source
    assert "app_port = get_app_port()" in settings_source
    assert "/api/launcher-ping" in app_source
    assert '"port": _APP_PORT' in app_source


def test_settings_lazy_tabs_use_package_imports():
    settings_source = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")

    assert '__import__("row_bot.ui.buddy"' in settings_source
    assert '__import__("row_bot.ui.mcp_settings"' in settings_source
    assert '__import__("ui.' not in settings_source


def test_plugin_loader_preserves_public_plugin_api_import(monkeypatch):
    import sys

    import row_bot.plugins.api as plugin_api
    from row_bot.plugins import loader

    monkeypatch.delitem(sys.modules, "plugins", raising=False)
    monkeypatch.delitem(sys.modules, "plugins.api", raising=False)

    loader._install_plugin_api_compat_aliases()

    imported_api = __import__("plugins.api", fromlist=["PluginAPI"])
    assert imported_api is plugin_api
