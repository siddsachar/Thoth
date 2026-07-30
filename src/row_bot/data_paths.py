"""Shared filesystem paths for local app data during the Row-Bot rebrand."""

from __future__ import annotations

import os
from pathlib import Path

from row_bot.brand import APP_DATA_DIR_ENV, default_data_dir


def get_row_bot_target_data_dir(*, create: bool = True) -> Path:
    """Return the canonical Row-Bot target data directory."""
    path = Path(os.environ.get(APP_DATA_DIR_ENV) or default_data_dir())
    path = path.expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_row_bot_data_dir(*, create: bool = True) -> Path:
    """Return the currently active Row-Bot data directory."""
    path = Path(os.environ.get(APP_DATA_DIR_ENV) or default_data_dir())
    path = path.expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_tasks_db_path(*, create_parent: bool = True) -> Path:
    """Return the task database path."""
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "tasks.db"


def get_memory_db_path(*, create_parent: bool = True) -> Path:
    """Return the memory database path."""
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "memory.db"


def get_threads_db_path(*, create_parent: bool = True) -> Path:
    """Return the thread metadata/checkpoint database path."""
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "threads.db"


def get_access_db_path(*, create_parent: bool = True) -> Path:
    """Return the access database path.

    The physical filename remains ``mobile.db`` for a safe in-place migration
    of existing companion devices.
    """
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "mobile.db"


def get_mobile_db_path(*, create_parent: bool = True) -> Path:
    """Compatibility alias for :func:`get_access_db_path`."""
    return get_access_db_path(create_parent=create_parent)


def describe_data_paths() -> dict[str, str]:
    """Return support-friendly local data paths."""
    data_dir = get_row_bot_data_dir()
    return {
        "data_dir": str(data_dir),
        "tasks_db": str(data_dir / "tasks.db"),
        "memory_db": str(data_dir / "memory.db"),
        "threads_db": str(data_dir / "threads.db"),
        "access_db": str(data_dir / "mobile.db"),
        "mobile_db": str(data_dir / "mobile.db"),
        "logs_dir": str(data_dir / "logs"),
    }
