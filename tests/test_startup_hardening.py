import importlib
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import textwrap
import builtins
from types import ModuleType, SimpleNamespace

from PIL import Image
import row_bot.launcher as launcher
import row_bot.startup_diagnostics as startup_diagnostics


def test_preflight_reports_broken_torchcodec(monkeypatch, caplog):
    def fake_find_spec(package):
        return object() if package == "torchcodec" else None

    def fake_import_module(package):
        if package == "torchcodec":
            raise OSError("libtorchcodec_core4.dll could not load")
        raise AssertionError(package)

    patched = []

    monkeypatch.setattr(startup_diagnostics.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(startup_diagnostics.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(startup_diagnostics, "_disable_transformers_torchcodec", lambda log: patched.append(log))

    with caplog.at_level(logging.WARNING):
        issues = startup_diagnostics.preflight_optional_native_packages()

    assert len(issues) == 1
    assert issues[0].package == "torchcodec"
    assert "libtorchcodec_core4.dll" in issues[0].error
    assert "pip uninstall -y torchcodec" in issues[0].recovery_hint
    assert patched
    assert "Optional package 'torchcodec' is installed but cannot be imported" in caplog.text


def test_launcher_hints_for_torchcodec_dll_failure():
    hints = launcher._startup_failure_hints(
        "OSError: Could not load this library: E:\\Row-Bot\\Row-Bot\\python\\Lib\\site-packages\\torchcodec\\libtorchcodec_core4.dll",
        python_executable="E:\\Row-Bot\\Row-Bot\\python\\python.exe",
    )

    assert any("broken optional TorchCodec" in hint for hint in hints)
    assert any('"E:\\Row-Bot\\Row-Bot\\python\\python.exe" -m pip uninstall -y torchcodec' in hint for hint in hints)


def test_launcher_hints_for_linux_opencv_native_failure():
    hints = launcher._startup_failure_hints(
        "ImportError: libGL.so.1: cannot open shared object file while importing cv2"
    )

    assert any("OpenCV/Linux native dependency" in hint for hint in hints)
    assert any("libgl1" in hint for hint in hints)


def test_launcher_hints_for_numpy_x86_v2_failure():
    hints = launcher._startup_failure_hints(
        "RuntimeError: NumPy was built with baseline optimizations: "
        "(X86_V2) but your machine doesn't support: (X86_V2)."
    )

    assert any("NumPy/native wheel startup failure" in hint for hint in hints)
    assert any("x86-64-v2" in hint for hint in hints)


def test_launcher_ignores_benign_faiss_avx2_fallback():
    hints = launcher._startup_failure_hints(
        "Loading faiss with AVX2 support.\n"
        "Could not load library with AVX2 support due to ModuleNotFoundError.\n"
        "Loading faiss.\n"
        "Successfully loaded faiss."
    )

    assert not any("FAISS native import failure" in hint for hint in hints)


def test_launcher_hints_for_real_faiss_import_failure():
    hints = launcher._startup_failure_hints(
        "Traceback while importing faiss\n"
        "ImportError: DLL load failed while importing swigfaiss"
    )

    assert any("FAISS native import failure" in hint for hint in hints)


def test_launcher_logs_app_tail_on_startup_failure(tmp_path, caplog):
    log_path = tmp_path / "row_bot_app.log"
    log_path.write_text("line one\nTraceback\nImportError: libGL.so.1 missing\n", encoding="utf-8")
    server = launcher._RowBotProcess(port=8123)
    server._log_file = log_path
    server._proc = None

    with caplog.at_level(logging.ERROR):
        launcher._log_startup_failure_context(server, 8123, "app process exited before readiness")

    assert "failed to become ready on port 8123" in caplog.text
    assert "ImportError: libGL.so.1 missing" in caplog.text
    assert "OpenCV/Linux native dependency" in caplog.text


def test_startup_timeout_env(monkeypatch):
    monkeypatch.setenv("ROW_BOT_STARTUP_TIMEOUT", "180")
    assert launcher._startup_timeout() == 180

    monkeypatch.setenv("ROW_BOT_STARTUP_TIMEOUT", "not-a-number")
    assert launcher._startup_timeout() == 120


def test_preflight_handles_real_broken_optional_package_subprocess(tmp_path):
    package_dir = tmp_path / "torchcodec"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        'raise OSError("Could not load this library: libtorchcodec_core4.dll")\n',
        encoding="utf-8",
    )

    code = textwrap.dedent(
        """
        import logging
        import row_bot.startup_diagnostics as startup_diagnostics

        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        issues = startup_diagnostics.preflight_optional_native_packages()
        print(len(issues))
        print(issues[0].package)
        print(issues[0].recovery_hint)
        """
    )
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(tmp_path), str(root / "src"), str(root), env.get("PYTHONPATH", "")) if part
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "1" in result.stdout
    assert "torchcodec" in result.stdout
    assert "pip uninstall -y torchcodec" in result.stdout
    assert "Optional package 'torchcodec' is installed but cannot be imported" in result.stderr


def test_app_imports_with_startup_preflight():
    app_module = importlib.import_module("row_bot.app")

    assert hasattr(app_module, "_APP_PORT")


def test_startup_speed_imports_are_lazy_source_contract():
    app_src = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    launcher_src = Path("src/row_bot/launcher.py").read_text(encoding="utf-8")
    state_src = Path("src/row_bot/ui/state.py").read_text(encoding="utf-8")
    agent_src = Path("src/row_bot/agent.py").read_text(encoding="utf-8")
    discord_src = Path("src/row_bot/channels/discord_channel.py").read_text(encoding="utf-8")
    smoke_src = Path("scripts/smoke_app.py").read_text(encoding="utf-8")

    assert "row_bot_legacy_rebrand" not in app_src
    assert "ensure_legacy_rebrand_migration" not in app_src
    assert "post_migration" not in app_src
    assert "row_bot_legacy_rebrand" not in launcher_src
    assert "ensure_legacy_rebrand_migration" not in launcher_src

    assert not any(
        line == "from row_bot.agent import get_token_usage"
        for line in app_src.splitlines()
    )
    assert "from row_bot.tools.vision_tool import set_vision_service" not in state_src
    assert "from row_bot.vision_runtime import set_vision_service" in state_src
    assert "time.sleep(0.5)" not in discord_src
    assert "await asyncio.sleep(0.5)" in discord_src
    assert "--wait-startup-ready" in smoke_src

    assert not any(
        line == "from langgraph.prebuilt import create_react_agent"
        for line in agent_src.splitlines()
    )
    assert "def create_react_agent" in agent_src


def test_channel_adapters_do_not_import_agent_at_module_import_time():
    for path in [
        Path("src/row_bot/channels/telegram.py"),
        Path("src/row_bot/channels/slack.py"),
        Path("src/row_bot/channels/sms.py"),
        Path("src/row_bot/channels/discord_channel.py"),
        Path("src/row_bot/channels/whatsapp.py"),
    ]:
        src = path.read_text(encoding="utf-8")
        assert not any(line == "import row_bot.agent as agent_mod" for line in src.splitlines())
        assert "def _agent_mod" in src


def test_auto_start_channels_are_scheduled_in_background(monkeypatch):
    app_module = importlib.import_module("row_bot.app")
    from row_bot.channels import registry as channel_registry

    invalidations: list[str] = []
    monkeypatch.setattr(channel_registry, "clear_agent_cache_if_loaded", lambda: invalidations.append("clear"))

    class FakeState:
        startup_warnings: list[str] = []

    class FakeChannel:
        name = "fake"
        display_name = "Fake"

        def __init__(self) -> None:
            self.started = False

        async def start(self) -> bool:
            await asyncio.sleep(0)
            self.started = True
            return True

    async def run_check() -> None:
        channel = FakeChannel()
        task = app_module._schedule_auto_start_channels([channel], FakeState())
        assert task is not None
        assert channel.started is False
        await task
        assert channel.started is True
        assert invalidations == ["clear"]

    asyncio.run(run_check())


def test_sms_webhook_registration_is_scheduled_in_background(monkeypatch):
    import row_bot.channels.sms as sms

    calls: list[str] = []
    monkeypatch.setattr(sms, "_auto_register_twilio_webhook", lambda url: calls.append(url))

    async def run_check() -> None:
        task = sms._schedule_twilio_webhook_registration("https://example.test")
        assert task is not None
        assert calls == []
        await task
        assert calls == ["https://example.test"]

    asyncio.run(run_check())


def test_app_import_survives_broken_cv2_module(tmp_path):
    fake_cv2 = tmp_path / "cv2.py"
    fake_cv2.write_text('raise OSError("libGL.so.1: cannot open shared object file")\n', encoding="utf-8")
    code = "import row_bot.app; print('app-import-ok')"
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(tmp_path), str(root / "src"), str(root), env.get("PYTHONPATH", "")) if part
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "app-import-ok" in result.stdout


def test_vision_degrades_when_cv2_native_import_fails(monkeypatch):
    import row_bot.vision as vision

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise OSError("libGL.so.1: cannot open shared object file")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(vision, "_cv2_mod", None)
    monkeypatch.setattr(vision, "_cv2_error", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    svc = vision.VisionService()

    assert svc.model
    assert vision.list_cameras() == []
    assert vision.capture_frame() is None
    status = vision.native_backend_status()
    assert status["opencv_available"] is False
    assert "libGL.so.1" in str(status["opencv_error"])


def test_windows_installer_replaces_embedded_python_on_install():
    iss = Path("installer/row_bot_setup.iss").read_text(encoding="utf-8")

    assert "[InstallDelete]" in iss
    assert 'Type: filesandordirs; Name: "{app}\\python"' in iss
    assert 'Source: "..\\src\\row_bot\\*"' in iss
    assert Path("src/row_bot/startup_diagnostics.py").is_file()


def test_windows_installer_build_verifies_tk_runtime():
    build_script = Path("installer/build_installer.ps1").read_text(encoding="utf-8")
    release_workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'WINDOWS_PYTHON_VERSION: "3.13.2"' in release_workflow
    assert 'python-version: ${{ env.WINDOWS_PYTHON_VERSION }}' in release_workflow
    assert '-PythonVersion "${{ env.WINDOWS_PYTHON_VERSION }}"' in release_workflow
    assert '$SysPyVersion -ne $PythonVersion' in build_script
    assert 'Resolve-TkSourceFile "_tkinter.pyd"' in build_script
    assert 'Resolve-TkSourceFile "zlib1.dll"' in build_script
    assert '$env:PATH = (Join-Path $PythonDir "Scripts") + ";" + $PythonDir + ";" + $env:PATH' in build_script
    assert 'os.add_dll_directory(py_dir)' in build_script
    assert 'import _tkinter' in build_script
    assert 'import tkinter' in build_script
    assert 'Embedded tkinter verified' in build_script


def test_windows_update_install_starts_handoff_before_quit(tmp_path, monkeypatch):
    import row_bot.updater as updater

    installer = tmp_path / "Row-Bot-test.exe"
    installer.write_bytes(b"installer")
    calls = []

    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updater, "verify_os_signature", lambda _path: (True, "ok"))
    monkeypatch.setattr(updater, "_launch_windows_update_handoff", lambda _path: calls.append("handoff"))
    monkeypatch.setitem(
        sys.modules,
        "row_bot.launcher",
        SimpleNamespace(quit_for_update=lambda: calls.append("quit")),
    )

    updater.install_and_restart(installer)

    assert calls == ["handoff", "quit"]


def test_windows_update_handoff_helper_command_is_detached(tmp_path, monkeypatch):
    import row_bot.updater as updater

    installer = tmp_path / "Row-Bot-test.exe"
    installer.write_bytes(b"installer")
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append((cmd, kwargs))

    monkeypatch.setenv("ROW_BOT_PORT", "8123")
    monkeypatch.setattr(updater.subprocess, "Popen", FakePopen)

    updater._launch_windows_update_handoff(installer)

    cmd, kwargs = calls[0]
    assert cmd[:3] == [sys.executable, "-m", "row_bot.update_handoff"]
    assert "--installer" in cmd
    assert "--app-pid" in cmd
    assert "--launcher-pid" in cmd
    assert "--port" in cmd and "8123" in cmd
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_splash_helper_immediate_failure_returns_quickly(tmp_path, monkeypatch, caplog):
    class FakePopen:
        pid = 101

        def __init__(self, cmd, **kwargs):  # noqa: ARG002
            kwargs["stdout"].write("ImportError: DLL load failed while importing _tkinter\n")

        def poll(self):
            return 1

    monkeypatch.setattr(launcher, "_row_bot_data_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.delenv("ROW_BOT_SPLASH_CONSOLE_FALLBACK", raising=False)
    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)

    with caplog.at_level(logging.WARNING):
        assert launcher._show_splash(port=8123, timeout=1.0) is None

    assert "splash_tk helper exited before ready" in caplog.text
    assert "DLL load failed" in caplog.text


def test_window_mode_picker_windows_defaults_without_console(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "_row_bot_data_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.delenv("ROW_BOT_WINDOW_MODE_CONSOLE_FALLBACK", raising=False)
    monkeypatch.setattr(launcher, "_start_launcher_helper", lambda **kwargs: None)
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("console fallback should not start")),
    )
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("console fallback should not run")),
    )

    assert launcher._ask_window_mode() == "native"


def test_window_mode_picker_uses_gui_result(tmp_path, monkeypatch):
    class FakeProc:
        pid = 202

        def wait(self, timeout=None):  # noqa: ARG002
            return 0

        def poll(self):
            return None

    def fake_start_helper(**kwargs):
        Path(kwargs["args"][3]).write_text("browser\n", encoding="utf-8")
        Path(kwargs["ready_marker"]).write_text("ready\n", encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(launcher, "_row_bot_data_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher, "_start_launcher_helper", fake_start_helper)

    assert launcher._ask_window_mode() == "browser"


def test_main_requests_early_splash_before_tray_start(monkeypatch):
    calls = []

    class FakeSplashProc:
        def poll(self):
            return None

    class FakeTray:
        def __init__(self, **kwargs):
            calls.append(("tray_init", kwargs))

        def run(self):
            calls.append(("tray_run", launcher._claim_early_splash() is not None))

    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "_show_splash", lambda port: calls.append(("splash", port)) or FakeSplashProc())
    monkeypatch.setattr(launcher, "_has_display_server", lambda: True)
    monkeypatch.setattr(launcher, "RowBotTray", FakeTray)
    monkeypatch.setattr(launcher, "_ACTIVE_TRAY", None)
    monkeypatch.setattr(launcher, "_EARLY_SPLASH_PROC", None)
    monkeypatch.delenv("ROW_BOT_HOST", raising=False)

    launcher.main(["--no-ollama"])

    assert calls[0][0] == "splash"
    assert calls[1][0] == "tray_init"
    assert calls[1][1]["host"] == "127.0.0.1"
    assert ("tray_run", True) in calls


def test_main_passes_cli_host_over_environment_to_tray(monkeypatch):
    captured = {}

    class FakeTray:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "RowBotTray", FakeTray)
    monkeypatch.setattr(launcher, "_ACTIVE_TRAY", None)
    monkeypatch.setenv("ROW_BOT_HOST", "0.0.0.0")

    launcher.main(
        [
            "--no-splash",
            "--no-ollama",
            "--host",
            "192.168.1.20",
        ]
    )

    assert captured["host"] == "192.168.1.20"
    assert captured["ran"] is True


def test_tray_builds_server_with_resolved_environment_host(monkeypatch):
    captured = {}

    class FakeServer:
        def __init__(self, port, host=None):
            captured["port"] = port
            captured["host"] = host

    monkeypatch.setenv("ROW_BOT_HOST", "0.0.0.0")
    monkeypatch.setattr(launcher, "_RowBotProcess", FakeServer)
    monkeypatch.setattr(launcher, "_create_tray_backend", lambda _entries: object())

    tray = launcher.RowBotTray(no_ollama=True)

    assert tray._host == "0.0.0.0"
    assert captured == {"port": 8080, "host": "0.0.0.0"}


def _install_fake_appkit(monkeypatch, events, setup_ready: threading.Event | None = None):
    class FakeNSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    class FakeObjC(ModuleType):
        def namedSelector(self, _selector):
            return lambda func: func

    class FakeApp:
        def __init__(self):
            self.activation_policy = None
            self.run_called = False
            self.stopped = False

        def setActivationPolicy_(self, policy):
            self.activation_policy = policy
            events.append("activation_policy")

        def run(self):
            events.append("app_run")
            self.run_called = True
            if setup_ready is not None:
                assert setup_ready.wait(1.0)

        def stop_(self, _sender):
            self.stopped = True

        def postEvent_atStart_(self, _event, _at_start):
            events.append("post_stop_event")

    class FakeButton:
        def __init__(self):
            self.images = []
            self.tooltips = []
            self.hidden = None
            self.title = ""
            self.image_positions = []

        def setImage_(self, image):
            self.images.append(image)

        def setImagePosition_(self, value):
            self.image_positions.append(value)

        def setTitle_(self, value):
            self.title = value

        def setToolTip_(self, value):
            self.tooltips.append(value)

        def setHidden_(self, value):
            self.hidden = value

    class FakeStatusItem:
        def __init__(self):
            self.button_obj = FakeButton()
            self.menu = None
            self.visible_values = []
            self.length_values = []

        def button(self):
            return self.button_obj

        def setMenu_(self, menu):
            self.menu = menu

        def setVisible_(self, visible):
            self.visible_values.append(visible)
            events.append(("visible", visible))

        def setLength_(self, length):
            self.length_values.append(length)
            events.append(("length", length))

    class FakeStatusBar:
        def __init__(self):
            self.status_item = FakeStatusItem()
            self.removed = []

        def statusItemWithLength_(self, length):
            events.append(("status_item", length))
            return self.status_item

        def thickness(self):
            return 22

        def removeStatusItem_(self, item):
            self.removed.append(item)

    class FakeMenu:
        def __init__(self):
            self.title = ""
            self.items = []

        @classmethod
        def alloc(cls):
            return cls()

        def initWithTitle_(self, title):
            self.title = title
            return self

        def setAutoenablesItems_(self, value):
            self.autoenables = value

        def addItem_(self, item):
            self.items.append(item)

    class FakeMenuItem:
        def __init__(self):
            self.title = ""
            self.target = None
            self.tag_value = None
            self.enabled = True
            self.separator = False

        @classmethod
        def alloc(cls):
            return cls()

        @classmethod
        def separatorItem(cls):
            item = cls()
            item.separator = True
            return item

        def initWithTitle_action_keyEquivalent_(self, title, action, key):
            self.title = title
            self.action = action
            self.key = key
            return self

        def setTarget_(self, target):
            self.target = target

        def setTag_(self, tag):
            self.tag_value = tag

        def tag(self):
            return self.tag_value

        def setEnabled_(self, enabled):
            self.enabled = enabled

    class FakeNSImage:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithData_(self, data):
            self.data = data
            return self

    fake_app = FakeApp()
    fake_status_bar = FakeStatusBar()

    fake_appkit = ModuleType("AppKit")
    fake_appkit.NSApplication = SimpleNamespace(sharedApplication=lambda: fake_app)
    fake_appkit.NSStatusBar = SimpleNamespace(systemStatusBar=lambda: fake_status_bar)
    fake_appkit.NSVariableStatusItemLength = -1
    fake_appkit.NSApplicationActivationPolicyAccessory = 1
    fake_appkit.NSImageLeft = 2
    fake_appkit.NSMenu = FakeMenu
    fake_appkit.NSMenuItem = FakeMenuItem
    fake_appkit.NSImage = FakeNSImage
    fake_appkit.NSEvent = SimpleNamespace()
    fake_appkit.NSApplicationDefined = 15
    fake_appkit.NSPoint = lambda x, y: (x, y)

    fake_foundation = ModuleType("Foundation")
    fake_foundation.NSObject = FakeNSObject
    fake_foundation.NSData = lambda payload: payload

    fake_apphelper = ModuleType("PyObjCTools.AppHelper")
    fake_apphelper.callAfter = lambda func, *args, **kwargs: func(*args, **kwargs)
    fake_pyobjc_tools = ModuleType("PyObjCTools")
    fake_pyobjc_tools.AppHelper = fake_apphelper

    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
    monkeypatch.setitem(sys.modules, "objc", FakeObjC("objc"))
    monkeypatch.setitem(sys.modules, "PyObjCTools", fake_pyobjc_tools)
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", fake_apphelper)
    monkeypatch.setattr(launcher, "_MAC_STATUS_ITEM_DELEGATE_CLASS", None, raising=False)
    monkeypatch.setattr(launcher, "_get_icon", lambda state: Image.new("RGBA", (64, 64), (0, 255, 0, 255)))
    return SimpleNamespace(app=fake_app, status_bar=fake_status_bar)


def test_macos_native_status_item_is_visible_before_launcher_startup(monkeypatch):
    events = []
    setup_ready = threading.Event()
    fake = _install_fake_appkit(monkeypatch, events, setup_ready)

    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.delenv("ROW_BOT_MAC_TRAY_BACKEND", raising=False)
    monkeypatch.setattr(launcher, "_launch_event", lambda event, **fields: events.append((event, fields)))

    tray = launcher.RowBotTray(no_ollama=True)
    monkeypatch.setattr(tray, "_run_startup_sequence", lambda: events.append("startup") or setup_ready.set())

    tray.run()

    assert isinstance(tray._icon, launcher._MacStatusItemBackend)
    assert fake.app.run_called
    assert fake.status_bar.status_item.visible_values == [False, True]
    assert fake.status_bar.status_item.length_values
    assert all(value >= launcher._MAC_STATUS_ITEM_MIN_LENGTH for value in fake.status_bar.status_item.length_values)
    assert fake.status_bar.status_item.button_obj.title == "RB"
    assert fake.status_bar.status_item.button_obj.images
    assert ("tray_status_item_visible", {"backend": "appkit", "visible": True}) in events
    assert events.index(("visible", True)) < events.index("startup")


def test_native_macos_status_item_wires_menu_callbacks(monkeypatch):
    events = []
    fake = _install_fake_appkit(monkeypatch, events)
    calls = []
    menu_entries = (
        launcher._TrayMenuEntry("Open Row-Bot", lambda *_args: calls.append("open"), default=True),
        launcher._tray_separator(),
        launcher._TrayMenuEntry("Quit", lambda *_args: calls.append("quit")),
    )
    monkeypatch.setattr(launcher, "_launch_event", lambda *args, **kwargs: None)

    backend = launcher._MacStatusItemBackend(menu_entries)

    menu_items = fake.status_bar.status_item.menu.items
    assert [item.title for item in menu_items if not item.separator] == ["Open Row-Bot", "Quit"]
    backend._activate_menu_item(menu_items[0])
    backend._activate_menu_item(menu_items[2])
    assert calls == ["open", "quit"]


def test_macos_appkit_import_failure_falls_back_to_pystray(monkeypatch):
    events = []

    class FakeMenu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class FakeMenuItem:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeIcon:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.visible = False

        def run(self, setup=None):
            if setup is not None:
                setup(self)

        def stop(self):
            pass

    fake_pystray = SimpleNamespace(Menu=FakeMenu, MenuItem=FakeMenuItem, Icon=FakeIcon)
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.delenv("ROW_BOT_MAC_TRAY_BACKEND", raising=False)
    monkeypatch.setattr(launcher, "_launch_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(launcher, "_get_icon", lambda state: object())

    real_import = builtins.__import__

    def _raise_for_appkit(name, *args, **kwargs):
        if name == "AppKit":
            raise ImportError("no AppKit")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _raise_for_appkit)

    backend = launcher._create_tray_backend((launcher._TrayMenuEntry("Quit", lambda *_args: None),))

    assert isinstance(backend, launcher._PystrayTrayBackend)
    assert any(event == "tray_backend_import_failed" for event, _fields in events)
    assert any(event == "tray_backend_fallback" for event, _fields in events)


def test_macos_pystray_fallback_sets_visible_before_launcher_startup(monkeypatch):
    events = []

    class FakeMenu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class FakeMenuItem:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeIcon:
        def __init__(self, **kwargs):
            events.append("icon_init")
            self.kwargs = kwargs
            self._visible = False

        @property
        def visible(self):
            return self._visible

        @visible.setter
        def visible(self, value):
            self._visible = value
            events.append(("visible", value))

        def run(self, setup=None):
            events.append("icon_run")
            assert setup is not None
            setup(self)

        def stop(self):
            events.append("icon_stop")

    fake_pystray = SimpleNamespace(Menu=FakeMenu, MenuItem=FakeMenuItem, Icon=FakeIcon)
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setenv("ROW_BOT_MAC_TRAY_BACKEND", "pystray")
    monkeypatch.setattr(launcher, "_get_icon", lambda state: object())
    monkeypatch.setattr(launcher, "_launch_event", lambda *args, **kwargs: None)

    tray = launcher.RowBotTray(no_ollama=True)
    monkeypatch.setattr(tray, "_run_startup_sequence", lambda: events.append("startup"))

    tray.run()

    assert events[:2] == ["icon_init", "icon_run"]
    assert ("visible", True) in events
    assert events.index("icon_run") < events.index("startup")
    assert events.index(("visible", True)) < events.index("startup")


def test_launcher_state_records_native_window_control(tmp_path, monkeypatch):
    events = []
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "_launch_event", lambda event, **fields: events.append((event, fields)))

    launcher._write_launcher_state(
        port=8091,
        mode="native",
        owns_server=True,
        window_control_port=18091,
        window_pid=12345,
    )

    state_path = tmp_path / "launcher_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["app"] == launcher.APP_PING_ID
    assert state["port"] == 8091
    assert state["mode"] == "native"
    assert state["owns_server"] is True
    assert state["window_control_port"] == 18091
    assert state["window_pid"] == 12345
    assert any(event == "launcher_state_written" for event, _fields in events)

    launcher._clear_launcher_state()

    assert not state_path.exists()


def test_launcher_state_clear_preserves_other_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "_launch_event", lambda *args, **kwargs: None)
    state_path = tmp_path / "launcher_state.json"
    state_path.write_text(json.dumps({"app": launcher.APP_PING_ID, "session": "other"}), encoding="utf-8")

    launcher._clear_launcher_state()

    assert state_path.exists()


def test_direct_native_mode_writes_window_state(tmp_path, monkeypatch):
    events = []
    started = []
    opened = []

    class FakeServer:
        def __init__(self, port, host=None):
            self.port = port
            self.host = host
            self._proc = SimpleNamespace(pid=1111)

        def start(self, port=None, host=None):
            started.append((port, host))

        def stop(self):
            pass

        @property
        def is_alive(self):
            return True

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROW_BOT_HOST", raising=False)
    monkeypatch.setattr(launcher, "_launch_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(launcher, "_maybe_start_ollama", lambda **_kwargs: None)
    monkeypatch.setattr(launcher, "_select_app_port", lambda preferred: (8092, False))
    monkeypatch.setattr(launcher, "_claim_early_splash", lambda: None)
    monkeypatch.setattr(launcher, "_show_splash", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_stop_launcher_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher, "_has_display_server", lambda: True)
    monkeypatch.setattr(launcher, "_find_free_port", lambda start, max_tries=50: 18092)
    monkeypatch.setattr(launcher, "_RowBotProcess", FakeServer)
    monkeypatch.setattr(
        launcher,
        "_block_until_interrupted",
        lambda server, owns_server, **_kwargs: None,
    )

    def fake_open_window(port, control_port=None):
        opened.append((port, control_port))
        return SimpleNamespace(pid=2222)

    monkeypatch.setattr(launcher, "_open_window", fake_open_window)
    args = SimpleNamespace(
        port=8092,
        host=None,
        no_ollama=True,
        no_splash=True,
        server=False,
        no_open=False,
        native=True,
    )

    launcher._run_direct(args)

    assert started == [(8092, "127.0.0.1")]
    assert opened == [(8092, 18092)]
    state = json.loads((tmp_path / "launcher_state.json").read_text(encoding="utf-8"))
    assert state["port"] == 8092
    assert state["mode"] == "native"
    assert state["window_control_port"] == 18092
    assert state["window_pid"] == 2222
    assert any(event == "native_window_requested" for event, _fields in events)
    assert any(
        event == "server_spawned" and fields["host"] == "127.0.0.1"
        for event, fields in events
    )


def test_direct_monitor_tolerates_launcher_controlled_restart(monkeypatch):
    class FakeStopEvent:
        def __init__(self):
            self.waits = 0

        def wait(self, _timeout):
            self.waits += 1
            return self.waits > 1

    class RestartingServer:
        is_alive = False

        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    server = RestartingServer()
    monkeypatch.setattr(launcher.threading, "Event", FakeStopEvent)

    launcher._block_until_interrupted(
        server,
        owns_server=True,
        restart_in_progress=lambda: True,
    )

    assert server.stopped is True


def test_direct_restart_reloads_durable_host_when_cli_host_was_omitted(monkeypatch):
    started = []
    resolved_inputs = []
    callbacks = []

    class FakeServer:
        def __init__(self, _port, host=None):
            self._proc = SimpleNamespace(pid=1111)
            self.is_alive = True
            started.append(("init", host))

        def start(self, port=None, host=None):
            self.is_alive = True
            started.append((port, host))

        def stop(self):
            self.is_alive = False

    class FakeControl:
        def __init__(self, callback):
            callbacks.append(callback)

        def start(self):
            pass

        def stop(self):
            pass

        def child_environment(self):
            return {}

    def resolve_host(value):
        resolved_inputs.append(value)
        return "127.0.0.1" if len(resolved_inputs) == 1 else "0.0.0.0"

    monkeypatch.setattr(launcher, "_resolve_launch_host", resolve_host)
    monkeypatch.setattr(launcher, "_maybe_start_ollama", lambda **_kwargs: None)
    monkeypatch.setattr(
        launcher,
        "_select_app_port",
        lambda preferred: (preferred, False),
    )
    monkeypatch.setattr(launcher, "_claim_early_splash", lambda: None)
    monkeypatch.setattr(launcher, "_RowBotProcess", FakeServer)
    monkeypatch.setattr(launcher, "LauncherControlServer", FakeControl)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda _port: False)
    monkeypatch.setattr(
        launcher,
        "_write_launcher_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(launcher, "_clear_launcher_state", lambda: None)
    monkeypatch.setattr(
        launcher,
        "_stop_launcher_helper",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        launcher,
        "_block_until_interrupted",
        lambda *_args, **_kwargs: callbacks[0](),
    )
    args = SimpleNamespace(
        port=18100,
        host="127.0.0.1",
        _dynamic_host_input=None,
        no_ollama=True,
        no_splash=True,
        server=False,
        no_open=True,
        native=False,
    )

    launcher._run_direct(args)

    assert resolved_inputs == [None, None]
    assert started == [
        ("init", "127.0.0.1"),
        (18100, "127.0.0.1"),
        (18100, "0.0.0.0"),
    ]


def test_windows_tray_keeps_pystray_backend_and_menu(monkeypatch):
    events = []

    class FakeMenu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class FakeMenuItem:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeIcon:
        def __init__(self, **kwargs):
            events.append(("icon_init", kwargs))
            self.kwargs = kwargs
            self.icon = kwargs["icon"]
            self.title = kwargs["title"]

        def run(self, setup=None):
            events.append(("icon_run", setup))
            assert setup is None

        def stop(self):
            pass

    fake_pystray = SimpleNamespace(Menu=FakeMenu, MenuItem=FakeMenuItem, Icon=FakeIcon)
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "_get_icon", lambda state: object())
    monkeypatch.setattr(launcher, "_launch_event", lambda *args, **kwargs: None)

    tray = launcher.RowBotTray(no_ollama=True)
    monkeypatch.setattr(tray, "_run_startup_sequence", lambda: events.append("startup"))

    tray.run()

    assert isinstance(tray._icon, launcher._PystrayTrayBackend)
    assert events[0][0] == "icon_init"
    menu = events[0][1]["menu"]
    labels = [item.args[0] for item in menu.items if item is not FakeMenu.SEPARATOR]
    assert labels == ["Open Row-Bot", "Open in Browser", "Show Buddy", "Hide Buddy", "Quit"]
    assert events[-2:] == ["startup", ("icon_run", None)]


def test_update_handoff_helper_is_targeted_and_logged():
    source = Path("src/row_bot/update_handoff.py").read_text(encoding="utf-8")

    assert "update-handoff.log" in source
    assert '["taskkill", "/PID", str(pid), "/T", "/F"]' in source
    assert "/SILENT" in source
    assert "/CLOSEAPPLICATIONS" in source
    assert "/RESTARTAPPLICATIONS" in source
    assert "row_bot" not in source.lower().split("taskkill", 1)[1].split("]", 1)[0]


def test_launcher_splash_and_batch_startup_are_hardened():
    launcher_src = Path("src/row_bot/launcher.py").read_text(encoding="utf-8")
    batch_src = Path("installer/launch_row_bot.bat").read_text(encoding="utf-8")

    assert "launcher.log" in launcher_src
    assert "ROW_BOT_LAUNCH_TRACE" in launcher_src
    assert "splash_tk_exited" in launcher_src
    assert "ROW_BOT_SPLASH_CONSOLE_FALLBACK" in launcher_src
    assert "ROW_BOT_WINDOW_MODE_CONSOLE_FALLBACK" in launcher_src
    assert "Skipping Windows console window mode fallback" in launcher_src
    assert "helper_ready_timeout" in launcher_src
    assert "early_splash_requested" in launcher_src
    assert "skipping Windows console splash fallback" in launcher_src
    assert "ROW_BOT_BATCH_START_OLLAMA" in batch_src
    assert 'goto :launch_app' in batch_src
