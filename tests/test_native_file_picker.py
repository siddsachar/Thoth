import asyncio
from pathlib import Path

import launcher
from ui import helpers


def test_browse_file_prefers_pywebview_native_dialog(monkeypatch):
    selected = r"C:\Users\tester\Downloads\client_secret.json"

    async def fake_pywebview(title, initial_dir, filetypes):
        assert title == "Select credentials.json"
        assert filetypes == [("JSON files", "*.json")]
        return selected

    def fail_native(*_args):
        raise AssertionError("server-side fallback should not run in native mode")

    monkeypatch.setattr(helpers, "_pick_file_pywebview", fake_pywebview)
    monkeypatch.setattr(helpers, "_pick_file_native", fail_native)

    result = asyncio.run(
        helpers.browse_file(
            "Select credentials.json",
            r"C:\Users\tester\Downloads",
            [("JSON files", "*.json")],
        )
    )

    assert result == selected


def test_browse_file_falls_back_when_pywebview_unavailable(monkeypatch):
    selected = r"C:\Users\tester\Downloads\client_secret.json"

    async def no_pywebview(*_args):
        return None

    def fake_native(title, initial_dir, filetypes):
        assert title == "Select credentials.json"
        assert initial_dir == r"C:\Users\tester\Downloads"
        assert filetypes == [("JSON files", "*.json")]
        return selected

    monkeypatch.setattr(helpers, "_pick_file_pywebview", no_pywebview)
    monkeypatch.setattr(helpers, "_pick_file_native", fake_native)

    result = asyncio.run(
        helpers.browse_file(
            "Select credentials.json",
            r"C:\Users\tester\Downloads",
            [("JSON files", "*.json")],
        )
    )

    assert result == selected


def test_browse_folder_prefers_pywebview_native_dialog(monkeypatch):
    selected = r"C:\Users\tester\ThothData"

    async def fake_pywebview(title, initial_dir):
        assert title == "Select folder"
        assert initial_dir == r"C:\Users\tester"
        return selected

    def fail_native(*_args):
        raise AssertionError("server-side fallback should not run in native mode")

    monkeypatch.setattr(helpers, "_pick_folder_pywebview", fake_pywebview)
    monkeypatch.setattr(helpers, "_pick_folder_native", fail_native)

    result = asyncio.run(helpers.browse_folder("Select folder", r"C:\Users\tester"))

    assert result == selected


def test_launcher_pywebview_api_exposes_file_and_folder_dialogs():
    script = launcher._WINDOW_SCRIPT

    assert "def choose_file" in script
    assert "def choose_folder" in script
    assert "create_file_dialog" in script
    assert "webview.OPEN_DIALOG" in script
    assert "webview.FOLDER_DIALOG" in script
    assert "file_types=self._dialog_file_types(file_types)" in script


def test_google_credentials_browse_uses_shared_file_picker_and_copy():
    settings_src = Path("ui/settings.py").read_text(encoding="utf-8")

    assert "path = await browse_file(" in settings_src
    assert "Select credentials.json (or client_secret_*.json)" in settings_src
    assert "[(\"JSON files\", \"*.json\")]" in settings_src
    assert "shutil.copy2" in settings_src
    assert "cal_tool.set_config(\"credentials_path\", canonical)" in settings_src