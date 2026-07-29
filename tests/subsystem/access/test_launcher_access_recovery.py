from __future__ import annotations

from row_bot import launcher
from row_bot.data_paths import (
    describe_data_paths,
    get_access_db_path,
    get_mobile_db_path,
)


def test_access_database_keeps_mobile_filename_and_support_alias(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))

    assert get_access_db_path() == tmp_path / "mobile.db"
    assert get_mobile_db_path() == get_access_db_path()
    assert describe_data_paths()["access_db"] == str(tmp_path / "mobile.db")
    assert describe_data_paths()["mobile_db"] == str(tmp_path / "mobile.db")


def test_reset_all_local_databases_backs_up_access_sqlite_family(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "_timestamp", lambda: "20260727-120000")
    monkeypatch.setattr(
        "row_bot.tasks.ensure_task_schema",
        lambda **_kwargs: {"status": "ok"},
    )
    access_db = tmp_path / "mobile.db"
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"mobile.db{suffix}").write_text(
            f"access{suffix}",
            encoding="utf-8",
        )

    assert launcher._reset_all_local_dbs() == 0

    backup = tmp_path / "recovery" / "local-db-reset-20260727-120000"
    for suffix in ("", "-wal", "-shm"):
        assert (backup / f"mobile.db{suffix}").read_text(encoding="utf-8") == (
            f"access{suffix}"
        )
        assert not (tmp_path / f"mobile.db{suffix}").exists()
    assert access_db == get_access_db_path()


def test_restore_data_restores_access_sqlite_family_and_preserves_current_copy(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "_timestamp", lambda: "20260727-130000")
    source = tmp_path / "recovery" / "access-backup"
    source.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        (source / f"mobile.db{suffix}").write_text(
            f"backup{suffix}",
            encoding="utf-8",
        )
        (tmp_path / f"mobile.db{suffix}").write_text(
            f"current{suffix}",
            encoding="utf-8",
        )

    assert launcher._restore_data("access-backup") == 0

    pre_restore = tmp_path / "recovery" / "pre-restore-20260727-130000"
    for suffix in ("", "-wal", "-shm"):
        assert (tmp_path / f"mobile.db{suffix}").read_text(encoding="utf-8") == (
            f"backup{suffix}"
        )
        assert (pre_restore / f"mobile.db{suffix}").read_text(
            encoding="utf-8",
        ) == f"current{suffix}"
