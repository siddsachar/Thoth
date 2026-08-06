from __future__ import annotations

import importlib
from pathlib import Path

LEGACY_DATA_DIR_ENV = "THOTH_DATA_DIR"


def test_brand_constants_define_row_bot_identity():
    import row_bot.brand as brand
    from row_bot.version import __version__

    assert __version__ == "4.6.0"
    assert brand.APP_DISPLAY_NAME == "Row-Bot"
    assert brand.APP_SLUG == "row-bot"
    assert brand.APP_PYTHON_IDENTIFIER == "row_bot"
    assert brand.APP_BRAND_ACCENT == "#4F78A4"
    assert brand.APP_BRAND_ACCENT_RGB == "79, 120, 164"
    assert brand.APP_WEBSITE_URL == "https://row-bot.ai"
    assert brand.APP_REPOSITORY_URL == "https://github.com/siddsachar/row-bot"
    assert brand.APP_USER_AGENT == "Row-Bot/4.6.0"
    assert brand.UPDATER_USER_AGENT == "Row-Bot-Updater/4.6.0"
    assert brand.APP_DATA_DIR_ENV == "ROW_BOT_DATA_DIR"
    assert brand.APP_PORT_ENV == "ROW_BOT_PORT"
    assert brand.MACOS_APP_BUNDLE_NAME == "Row-Bot.app"
    assert brand.WINDOWS_INSTALLER_BASENAME == "Row-Bot"
    assert brand.LEGACY_WINDOWS_INSTALLER_BASENAME == "RowBotSetup"
    assert brand.MACOS_BUNDLE_ID == "ai.row-bot.assistant"
    assert brand.LINUX_DESKTOP_ID == "ai.row-bot.RowBot.desktop"
    assert brand.UPDATE_MANIFEST_MARKER == "row-bot-update-manifest"


def test_data_path_helpers_default_to_row_bot_target(tmp_path, monkeypatch):
    import row_bot.brand as brand
    import row_bot.data_paths as data_paths

    data_paths = importlib.reload(data_paths)

    monkeypatch.delenv("ROW_BOT_DATA_DIR", raising=False)
    monkeypatch.delenv(LEGACY_DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(brand.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(data_paths.Path, "home", classmethod(lambda cls: tmp_path))

    active = data_paths.get_row_bot_data_dir(create=False)
    target = data_paths.get_row_bot_target_data_dir(create=False)

    assert active == tmp_path / ".row-bot"
    assert target == tmp_path / ".row-bot"


def test_row_bot_data_dir_ignores_legacy_runtime_env(tmp_path, monkeypatch):
    import row_bot.data_paths as data_paths

    target = tmp_path / "target"
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(target))
    monkeypatch.setenv(LEGACY_DATA_DIR_ENV, str(legacy))

    data_paths = importlib.reload(data_paths)

    assert data_paths.get_row_bot_data_dir(create=False) == target
    assert data_paths.get_row_bot_target_data_dir(create=False) == target

    monkeypatch.delenv("ROW_BOT_DATA_DIR", raising=False)
    assert data_paths.get_row_bot_data_dir(create=False) != legacy


def test_test_harness_pins_row_bot_data_env_to_sandbox():
    import os

    row_bot_dir = Path(os.environ["ROW_BOT_DATA_DIR"]).resolve()

    assert ".tmp" in row_bot_dir.parts
    assert Path.cwd().resolve() in row_bot_dir.parents
