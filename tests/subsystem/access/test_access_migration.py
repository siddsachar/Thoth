from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3

import pytest

from row_bot.access.models import AccessProfile, TokenFormat
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore
from row_bot.access.tokens import hash_secret


NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)


def _legacy_database(path, *, secret: str = "legacy-secret-that-is-long-enough") -> str:
    verifier = hash_secret(secret)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE mobile_devices(
              id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              token_hash TEXT NOT NULL UNIQUE,
              token_salt TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_seen_at TEXT,
              revoked_at TEXT,
              user_agent TEXT,
              paired_from TEXT,
              access_mode TEXT,
              scopes_json TEXT NOT NULL
            );
            CREATE TABLE mobile_pairing_codes(
              id TEXT PRIMARY KEY,
              code_hash TEXT NOT NULL UNIQUE,
              code_salt TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              claimed_at TEXT,
              intended_origin TEXT,
              access_mode TEXT,
              failed_attempts INTEGER NOT NULL DEFAULT 0,
              locked_until TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO mobile_devices
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a" * 32,
                "Active phone",
                verifier.secret_hash,
                verifier.salt,
                NOW.isoformat(),
                None,
                None,
                "legacy-agent",
                "192.0.2.1",
                "lan",
                '["settings"]',
            ),
        )
        connection.execute(
            """
            INSERT INTO mobile_devices
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "b" * 32,
                "Revoked phone",
                hash_secret("another-legacy-secret").secret_hash,
                hash_secret("another-legacy-secret").salt,
                NOW.isoformat(),
                None,
                NOW.isoformat(),
                None,
                None,
                "lan",
                '["settings"]',
            ),
        )
        connection.execute(
            """
            INSERT INTO mobile_pairing_codes(
                id, code_hash, code_salt, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            ("c" * 32, "hash", "salt", NOW.isoformat(), NOW.isoformat()),
        )
    return secret


def test_legacy_devices_migrate_companion_only_and_tables_are_retained(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    secret = _legacy_database(path)
    store = AccessStore(path)
    store.ensure_schema()

    devices = {device.id: device for device in store.list_devices()}
    assert set(devices) == {"a" * 32, "b" * 32}
    assert all(device.profile is AccessProfile.COMPANION for device in devices.values())
    assert devices["b" * 32].revoked_at == NOW
    sessions = store.list_sessions()
    assert all(session.token_format is TokenFormat.LEGACY_RBD for session in sessions)
    assert all(session.expires_at > NOW for session in sessions)
    assert (
        AccessService(store).validate_legacy_session(
            f"rbd_{'a' * 32}.{secret}",
            now=NOW,
            touch=False,
        )
        is not None
    )
    assert (
        AccessService(store).validate_legacy_session(
            f"rbd_{'b' * 32}.another-legacy-secret",
            now=NOW,
            touch=False,
        )
        is None
    )
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "mobile_devices" in tables
        assert "mobile_pairing_codes" in tables
        assert connection.execute(
            "SELECT COUNT(*) FROM access_invitations"
        ).fetchone()[0] == 0
    assert store.recovery_copy_paths()


def test_migration_is_idempotent(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    _legacy_database(path)
    store = AccessStore(path)

    store.ensure_schema()
    first_instance = store.instance_id
    first_recovery = store.recovery_copy_paths()
    store.ensure_schema()

    assert len(store.list_devices()) == 2
    assert len(store.list_sessions()) == 2
    assert store.instance_id == first_instance
    assert store.recovery_copy_paths() == first_recovery


def test_version_one_schema_adds_per_client_invitation_failures(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    store = AccessStore(path)
    store.ensure_schema()
    instance_id = store.instance_id

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE access_invitation_failures")
        connection.execute("PRAGMA user_version = 1")

    store.ensure_schema()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'access_invitation_failures'"
        ).fetchone()
    assert store.instance_id == instance_id
    assert store.recovery_copy_paths()


def test_injected_migration_failure_rolls_back_schema_and_rows(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    _legacy_database(path)

    def fail_after_legacy(phase: str) -> None:
        if phase == "after_legacy":
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        AccessStore(path, migration_hook=fail_after_legacy).ensure_schema()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        access_tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'access_%'"
        ).fetchone()[0]
    assert access_tables == 0

    AccessStore(path).ensure_schema()
    assert len(AccessStore(path).list_devices()) == 2


def test_concurrent_schema_initialization_is_serialized(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    _legacy_database(path)

    def initialize(_index: int) -> str:
        store = AccessStore(path)
        store.ensure_schema()
        return store.instance_id

    with ThreadPoolExecutor(max_workers=6) as executor:
        instance_ids = list(executor.map(initialize, range(12)))

    assert len(set(instance_ids)) == 1
    assert len(AccessStore(path).list_devices()) == 2
    assert len(AccessStore(path).list_sessions()) == 2
