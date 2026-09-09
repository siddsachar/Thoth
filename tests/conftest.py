from __future__ import annotations

import builtins
import io
import os
import pathlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATA_DIR = PROJECT_ROOT / ".tmp" / "pytest_row_bot"
DEFAULT_TEST_TMP_DIR = PROJECT_ROOT / ".tmp" / "pytest_tmp"
LEGACY_DATA_DIR_NAME = ".thoth"
LIVE_LEGACY_DATA_DIR = Path.home() / LEGACY_DATA_DIR_NAME
LIVE_ROW_BOT_DATA_DIR = Path.home() / ".row-bot"


def _resolve_for_guard(path: Any) -> Path | None:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (TypeError, OSError, RuntimeError):
        return None


def _is_under(path: Path | None, root: Path) -> bool:
    if path is None:
        return False
    try:
        path.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _is_live_user_state_path(path: Any) -> bool:
    resolved = _resolve_for_guard(path)
    return _is_under(resolved, LIVE_LEGACY_DATA_DIR) or _is_under(resolved, LIVE_ROW_BOT_DATA_DIR)


def _set_default_test_data_env() -> None:
    os.environ["ROW_BOT_DATA_DIR"] = str(DEFAULT_TEST_DATA_DIR)


def _is_write_mode(mode: Any) -> bool:
    text = str(mode or "r")
    return any(flag in text for flag in ("w", "a", "x", "+"))


def _live_write_allowed() -> bool:
    return os.environ.get("ROW_BOT_ALLOW_LIVE_USER_STATE_WRITES") == "1"


def _raise_live_write(path: Any, operation: str) -> None:
    if _live_write_allowed():
        return
    raise AssertionError(
        f"pytest attempted to {operation} live app user state: {path}. "
        f"Use ROW_BOT_DATA_DIR under {DEFAULT_TEST_DATA_DIR.parent} for tests."
    )


# Establish a non-live data directory before test modules import app code. If a
# developer shell already points at live user state, override it for test safety.
existing_data_dir = os.environ.get("ROW_BOT_DATA_DIR")
if not existing_data_dir or _is_live_user_state_path(existing_data_dir):
    existing_data_dir = str(DEFAULT_TEST_DATA_DIR)
os.environ["ROW_BOT_DATA_DIR"] = existing_data_dir
DEFAULT_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_TEST_TMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMP", str(DEFAULT_TEST_TMP_DIR))
os.environ.setdefault("TEMP", str(DEFAULT_TEST_TMP_DIR))
os.environ.setdefault("ROW_BOT_TEST_MODE", "1")


_ORIGINAL_BUILTINS_OPEN = builtins.open
_ORIGINAL_IO_OPEN = io.open
_ORIGINAL_PATH_OPEN = pathlib.Path.open
_ORIGINAL_PATH_WRITE_TEXT = pathlib.Path.write_text
_ORIGINAL_PATH_WRITE_BYTES = pathlib.Path.write_bytes
_ORIGINAL_PATH_MKDIR = pathlib.Path.mkdir
_ORIGINAL_PATH_REPLACE = pathlib.Path.replace
_ORIGINAL_OS_REPLACE = os.replace
_ORIGINAL_SQLITE_CONNECT = sqlite3.connect
_ORIGINAL_MONKEYPATCH_SETENV = pytest.MonkeyPatch.setenv


def _guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
    if _is_write_mode(mode) and _is_live_user_state_path(file):
        _raise_live_write(file, f"open with mode {mode!r}")
    return _ORIGINAL_BUILTINS_OPEN(file, mode, *args, **kwargs)


def _guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
    if _is_write_mode(mode) and _is_live_user_state_path(file):
        _raise_live_write(file, f"io.open with mode {mode!r}")
    return _ORIGINAL_IO_OPEN(file, mode, *args, **kwargs)


def _guarded_path_open(self: pathlib.Path, mode: str = "r", *args: Any, **kwargs: Any):
    if _is_write_mode(mode) and _is_live_user_state_path(self):
        _raise_live_write(self, f"Path.open with mode {mode!r}")
    return _ORIGINAL_PATH_OPEN(self, mode, *args, **kwargs)


def _guarded_write_text(self: pathlib.Path, *args: Any, **kwargs: Any):
    if _is_live_user_state_path(self):
        _raise_live_write(self, "Path.write_text")
    return _ORIGINAL_PATH_WRITE_TEXT(self, *args, **kwargs)


def _guarded_write_bytes(self: pathlib.Path, *args: Any, **kwargs: Any):
    if _is_live_user_state_path(self):
        _raise_live_write(self, "Path.write_bytes")
    return _ORIGINAL_PATH_WRITE_BYTES(self, *args, **kwargs)


def _guarded_mkdir(self: pathlib.Path, *args: Any, **kwargs: Any):
    if _is_live_user_state_path(self):
        _raise_live_write(self, "Path.mkdir")
    return _ORIGINAL_PATH_MKDIR(self, *args, **kwargs)


def _guarded_path_replace(self: pathlib.Path, target: Any, *args: Any, **kwargs: Any):
    if _is_live_user_state_path(target):
        _raise_live_write(target, "Path.replace target")
    return _ORIGINAL_PATH_REPLACE(self, target, *args, **kwargs)


def _guarded_os_replace(src: Any, dst: Any, *args: Any, **kwargs: Any):
    if _is_live_user_state_path(dst):
        _raise_live_write(dst, "os.replace target")
    return _ORIGINAL_OS_REPLACE(src, dst, *args, **kwargs)


def _guarded_sqlite_connect(database: Any, *args: Any, **kwargs: Any):
    if _is_live_user_state_path(database):
        _raise_live_write(database, "sqlite3.connect")
    return _ORIGINAL_SQLITE_CONNECT(database, *args, **kwargs)


def _synced_setenv(self: pytest.MonkeyPatch, name: str, value: str, *args: Any, **kwargs: Any) -> None:
    _ORIGINAL_MONKEYPATCH_SETENV(self, name, value, *args, **kwargs)


builtins.open = _guarded_open
io.open = _guarded_io_open
pathlib.Path.open = _guarded_path_open
pathlib.Path.write_text = _guarded_write_text
pathlib.Path.write_bytes = _guarded_write_bytes
pathlib.Path.mkdir = _guarded_mkdir
pathlib.Path.replace = _guarded_path_replace
os.replace = _guarded_os_replace
sqlite3.connect = _guarded_sqlite_connect
pytest.MonkeyPatch.setenv = _synced_setenv


@pytest.fixture(autouse=True)
def _reset_test_data_env_between_tests():
    _set_default_test_data_env()
    yield
    _set_default_test_data_env()


@pytest.fixture(autouse=True)
def _isolate_desktop_notification_outputs(monkeypatch):
    # Exercise notification state/toasts normally while keeping deterministic
    # tests from showing OS alerts or playing sounds on the developer's desktop.
    from row_bot import notifications

    monkeypatch.setattr(notifications, "_desktop_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(notifications, "_play_sound", lambda *args, **kwargs: None)
