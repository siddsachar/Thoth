import importlib
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import launcher
import startup_diagnostics


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
        "OSError: Could not load this library: E:\\Thoth\\Thoth\\python\\Lib\\site-packages\\torchcodec\\libtorchcodec_core4.dll",
        python_executable="E:\\Thoth\\Thoth\\python\\python.exe",
    )

    assert any("broken optional TorchCodec" in hint for hint in hints)
    assert any('"E:\\Thoth\\Thoth\\python\\python.exe" -m pip uninstall -y torchcodec' in hint for hint in hints)


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
        import startup_diagnostics

        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        issues = startup_diagnostics.preflight_optional_native_packages()
        print(len(issues))
        print(issues[0].package)
        print(issues[0].recovery_hint)
        """
    )
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(tmp_path), root, env.get("PYTHONPATH", "")) if part
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
    app_module = importlib.import_module("app")

    assert hasattr(app_module, "_APP_PORT")


def test_windows_installer_replaces_embedded_python_on_install():
    iss = Path("installer/thoth_setup.iss").read_text(encoding="utf-8")

    assert "[InstallDelete]" in iss
    assert 'Type: filesandordirs; Name: "{app}\\python"' in iss
    assert 'Source: "..\\startup_diagnostics.py"' in iss
