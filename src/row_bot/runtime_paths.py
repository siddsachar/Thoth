"""Runtime path helpers for source and packaged Row-Bot layouts."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent
APP_ROOT = SRC_DIR.parent
APP_ROOT_ENV = "ROW_BOT_APP_ROOT"


def app_root() -> Path:
    """Return the repository root or installed app payload root."""

    configured = os.environ.get(APP_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return APP_ROOT


def app_path(*parts: str) -> Path:
    return app_root().joinpath(*parts)


def static_dir() -> Path:
    return app_path("static")


def sounds_dir() -> Path:
    return app_path("sounds")


def bundled_skills_dir() -> Path:
    return app_path("bundled_skills")


def tool_guides_dir() -> Path:
    return app_path("tool_guides")


def app_icon_path() -> Path:
    return app_path("row-bot.ico")
