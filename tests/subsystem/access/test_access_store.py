from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from row_bot.access.models import AccessProfile, SessionLifetime
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore, SCHEMA_VERSION
from row_bot.data_paths import (
    describe_data_paths,
    get_access_db_path,
    get_mobile_db_path,
)


NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)


def test_fresh_schema_has_separate_records_and_persistent_instance_id(tmp_path) -> None:
    store = AccessStore(tmp_path / "mobile.db")
    store.ensure_schema()
    original_instance_id = store.instance_id

    with sqlite3.connect(store.db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert {
        "access_meta",
        "access_devices",
        "access_sessions",
        "access_invitations",
        "access_invitation_failures",
        "access_events",
    } <= tables
    assert AccessStore(store.db_path).instance_id == original_instance_id

    invitation = AccessService(store).create_invitation(
        profile=AccessProfile.OWNER,
        intended_origin="https://row-bot.example",
        now=NOW,
    )
    claim = AccessService(store).claim_invitation(
        invitation.token,
        intended_origin="https://row-bot.example",
        display_name="Workstation",
        now=NOW + timedelta(seconds=1),
    )

    assert claim.device.id != claim.session.id
    assert claim.session.device_id == claim.device.id
    assert len(store.list_devices()) == 1
    assert len(store.list_sessions(device_id=claim.device.id)) == 1


def test_expiry_revocation_revoke_all_and_pruning(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    first = service.create_invitation(
        profile=AccessProfile.OWNER,
        intended_origin="https://row-bot.example",
        session_lifetime=SessionLifetime.TEMPORARY,
        now=NOW,
    )
    first_claim = service.claim_invitation(
        first.token,
        intended_origin="https://row-bot.example",
        display_name="Temporary",
        now=NOW,
    )
    second = service.create_invitation(
        profile=AccessProfile.COMPANION,
        intended_origin="https://row-bot.example",
        now=NOW,
    )
    second_claim = service.claim_invitation(
        second.token,
        intended_origin="https://row-bot.example",
        display_name="Phone",
        now=NOW,
    )

    assert service.validate_session(first_claim.session_token, now=NOW) is not None
    assert (
        service.validate_session(
            first_claim.session_token,
            now=NOW + timedelta(hours=12),
        )
        is None
    )
    assert service.revoke_session(second_claim.session.id, now=NOW) is True
    assert service.revoke_session(second_claim.session.id, now=NOW) is False
    assert service.validate_session(second_claim.session_token, now=NOW) is None

    third = service.create_invitation(
        profile=AccessProfile.OWNER,
        intended_origin="https://row-bot.example",
        now=NOW,
    )
    third_claim = service.claim_invitation(
        third.token,
        intended_origin="https://row-bot.example",
        display_name="Other",
        now=NOW,
    )
    assert service.revoke_all(now=NOW) == 2
    assert service.validate_session(third_claim.session_token, now=NOW) is None

    results = service.prune(
        now=NOW + timedelta(days=61),
        retention=timedelta(days=30),
    )
    assert results["sessions"] == 3
    assert results["invitations"] == 3
    assert results["events"] >= 3


def test_device_revoke_cascades_to_all_sessions(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    invitation = service.create_invitation(
        profile=AccessProfile.OWNER,
        intended_origin="https://row-bot.example",
        now=NOW,
    )
    claim = service.claim_invitation(
        invitation.token,
        intended_origin="https://row-bot.example",
        display_name="Laptop",
        now=NOW,
    )

    assert service.revoke_device(claim.device.id, now=NOW) is True
    assert service.revoke_device(claim.device.id, now=NOW) is False
    assert service.validate_session(claim.session_token, now=NOW) is None
    assert service.list_sessions(device_id=claim.device.id)[0].revoked_at == NOW


def test_audit_detail_is_bounded_and_redacts_secret_fields(tmp_path) -> None:
    store = AccessStore(tmp_path / "mobile.db")
    event = store.log_event(
        "test",
        effective_client="x" * 500,
        user_agent="u" * 500,
        detail={
            "token": "must-not-persist",
            "authorization": "must-not-persist",
            "safe": "y" * 1000,
        },
        now=NOW,
    )

    assert "token" not in event.detail
    assert "authorization" not in event.detail
    assert len(event.detail["safe"]) == 512
    assert event.effective_client == "x" * 128
    assert event.user_agent == "u" * 256


def test_access_data_path_keeps_physical_mobile_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))

    assert get_access_db_path() == tmp_path / "mobile.db"
    assert get_mobile_db_path() == get_access_db_path()
    assert describe_data_paths()["access_db"] == str(tmp_path / "mobile.db")
