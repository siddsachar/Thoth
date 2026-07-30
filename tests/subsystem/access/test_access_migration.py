from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from row_bot.access.models import (
    AccessDevice,
    AccessSession,
    SessionLifetime,
    TokenFormat,
)
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore, SCHEMA_VERSION
from row_bot.access.tokens import hash_secret


NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
ORIGIN = "https://row-bot.example"


def _legacy_database(path, *, secret: str = "legacy-secret-that-is-long-enough") -> str:
    verifier = hash_secret(secret)
    revoked_verifier = hash_secret("another-legacy-secret")
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
            "INSERT INTO mobile_devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "INSERT INTO mobile_devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "b" * 32,
                "Revoked phone",
                revoked_verifier.secret_hash,
                revoked_verifier.salt,
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


def _version_two_database(path) -> dict[str, str]:
    store = AccessStore(path)
    service = AccessService(store)
    owner = service.create_invitation(intended_origin=ORIGIN, now=NOW)
    owner_claim = service.claim_invitation(
        owner.token,
        intended_origin=ORIGIN,
        display_name="Owner laptop",
        now=NOW,
    )
    explicit = service.create_invitation(intended_origin=ORIGIN, now=NOW)
    explicit_claim = service.claim_invitation(
        explicit.token,
        intended_origin=ORIGIN,
        display_name="Restricted phone",
        now=NOW,
    )
    pending = service.create_invitation(intended_origin=ORIGIN, now=NOW)
    owner_pending = service.create_invitation(intended_origin=ORIGIN, now=NOW)

    legacy_secret = "reviewed-legacy-token-secret"
    verifier = hash_secret(legacy_secret)
    legacy_id = "d" * 32
    store.create_device_record(
        AccessDevice(
            id=legacy_id,
            display_name="Original mobile owner",
            created_at=NOW,
            last_seen_at=NOW,
            revoked_at=None,
            user_agent="legacy-agent",
            paired_from="192.0.2.20",
            access_route="lan",
            legacy_source_id=legacy_id,
        )
    )
    store.create_session_record(
        AccessSession(
            id="legacy-session",
            device_id=legacy_id,
            token_hash=verifier.secret_hash,
            token_salt=verifier.salt,
            token_format=TokenFormat.LEGACY_RBD,
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(days=30),
            revoked_at=None,
            lifetime=SessionLifetime.MIGRATED,
            replaced_by_session_id=None,
        )
    )

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE access_devices SET profile = 'companion' WHERE id IN (?, ?)",
            (explicit_claim.device.id, legacy_id),
        )
        connection.execute(
            "UPDATE access_invitations SET profile = 'companion' WHERE id = ?",
            (pending.invitation.id,),
        )
        connection.execute("PRAGMA user_version = 2")
    return {
        "owner_device": owner_claim.device.id,
        "owner_session": owner_claim.session.id,
        "explicit_device": explicit_claim.device.id,
        "explicit_session": explicit_claim.session.id,
        "pending_invitation": pending.invitation.id,
        "owner_invitation": owner_pending.invitation.id,
        "legacy_device": legacy_id,
        "legacy_secret": legacy_secret,
    }


def test_fresh_version_three_schema_writes_owner_storage_invariant(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    service = AccessService(AccessStore(path))
    created = service.create_invitation(intended_origin=ORIGIN, now=NOW)
    claim = service.claim_invitation(
        created.token,
        intended_origin=ORIGIN,
        display_name="Owner browser",
        now=NOW,
    )

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT profile FROM access_devices WHERE id = ?", (claim.device.id,)
        ).fetchone()[0] == "owner"
        assert connection.execute(
            "SELECT profile FROM access_invitations WHERE id = ?",
            (created.invitation.id,),
        ).fetchone()[0] == "owner"
    assert "profile" not in claim.device.to_public_dict()
    assert "profile" not in created.invitation.to_public_dict()


def test_unmigrated_legacy_mobile_devices_keep_owner_sessions_and_tables(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    secret = _legacy_database(path)
    store = AccessStore(path)
    store.ensure_schema()

    devices = {device.id: device for device in store.list_devices()}
    assert set(devices) == {"a" * 32, "b" * 32}
    assert devices["b" * 32].revoked_at == NOW
    sessions = store.list_sessions()
    assert all(session.token_format is TokenFormat.LEGACY_RBD for session in sessions)
    assert all(session.expires_at > NOW for session in sessions)
    authenticated = AccessService(store).validate_legacy_session(
        f"rbd_{'a' * 32}.{secret}",
        now=NOW,
        touch=False,
    )
    assert authenticated is not None
    assert authenticated.device.display_name == "Active phone"
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
        assert {"mobile_devices", "mobile_pairing_codes"} <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM access_invitations"
        ).fetchone()[0] == 0
        assert set(
            row[0] for row in connection.execute("SELECT profile FROM access_devices")
        ) == {"owner"}
    assert store.recovery_copy_paths()


def test_version_two_migration_preserves_legacy_and_revokes_explicit_companion(
    tmp_path,
) -> None:
    path = tmp_path / "mobile.db"
    ids = _version_two_database(path)
    store = AccessStore(path)
    store.ensure_schema()

    owner = store.get_device(ids["owner_device"])
    explicit = store.get_device(ids["explicit_device"])
    legacy = store.get_device(ids["legacy_device"])
    assert owner is not None and owner.revoked_at is None
    assert store.get_session(ids["owner_session"]).revoked_at is None
    assert explicit is not None and explicit.revoked_at is not None
    assert store.get_session(ids["explicit_session"]).revoked_at is not None
    assert legacy is not None and legacy.revoked_at is None
    assert (
        AccessService(store).validate_legacy_session(
            f"rbd_{ids['legacy_device']}.{ids['legacy_secret']}",
            now=NOW,
            touch=False,
        )
        is not None
    )
    assert store.get_invitation(ids["pending_invitation"]).cancelled_at is not None
    assert store.get_invitation(ids["owner_invitation"]).cancelled_at is None

    with sqlite3.connect(path) as connection:
        assert set(
            row[0] for row in connection.execute("SELECT profile FROM access_devices")
        ) == {"owner"}
        assert set(
            row[0]
            for row in connection.execute("SELECT profile FROM access_invitations")
        ) == {"owner"}
    events = store.recent_events(limit=200)
    assert any(
        event.event_type == "owner_repair_required"
        and event.device_id == ids["explicit_device"]
        for event in events
    )
    assert any(
        event.event_type == "invitation_cancelled_for_owner_migration"
        and event.invitation_id == ids["pending_invitation"]
        for event in events
    )


def test_version_two_migration_is_idempotent_and_secret_free(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    ids = _version_two_database(path)
    store = AccessStore(path)
    store.ensure_schema()
    first_instance = store.instance_id
    first_events = [(event.id, event.event_type) for event in store.recent_events(limit=200)]
    first_recovery = store.recovery_copy_paths()

    store.ensure_schema()

    assert store.instance_id == first_instance
    assert [(event.id, event.event_type) for event in store.recent_events(limit=200)] == first_events
    assert store.recovery_copy_paths() == first_recovery
    event_dump = " ".join(str(event.to_public_dict()) for event in store.recent_events())
    assert ids["legacy_secret"] not in event_dump


@pytest.mark.parametrize("phase", ["after_semantic_normalization", "before_commit"])
def test_injected_version_two_migration_failure_rolls_back_changes(
    tmp_path,
    phase,
) -> None:
    path = tmp_path / "mobile.db"
    ids = _version_two_database(path)

    def fail(selected_phase: str) -> None:
        if selected_phase == phase:
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        AccessStore(path, migration_hook=fail).ensure_schema()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT revoked_at FROM access_devices WHERE id = ?",
            (ids["explicit_device"],),
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT cancelled_at FROM access_invitations WHERE id = ?",
            (ids["pending_invitation"],),
        ).fetchone()[0] is None
    assert AccessStore(path).recovery_copy_paths()


def test_injected_legacy_migration_failure_rolls_back_schema_and_rows(tmp_path) -> None:
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
    assert AccessStore(path).recovery_copy_paths()

    AccessStore(path).ensure_schema()
    assert len(AccessStore(path).list_devices()) == 2


def test_concurrent_schema_initialization_is_serialized(tmp_path) -> None:
    path = tmp_path / "mobile.db"
    ids = _version_two_database(path)

    def initialize(_index: int) -> str:
        store = AccessStore(path)
        store.ensure_schema()
        return store.instance_id

    with ThreadPoolExecutor(max_workers=6) as executor:
        instance_ids = list(executor.map(initialize, range(12)))

    store = AccessStore(path)
    assert len(set(instance_ids)) == 1
    assert store.get_device(ids["explicit_device"]).revoked_at is not None
    assert len(
        [
            event
            for event in store.recent_events(limit=200)
            if event.event_type == "owner_repair_required"
        ]
    ) == 1
