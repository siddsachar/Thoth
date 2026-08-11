from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path

from row_bot.access.models import (
    AccessDevice,
    AccessSession,
    SessionLifetime,
    TokenFormat,
)
from row_bot.access.service import (
    AccessService,
    TRUSTED_SESSION_RENEWAL_WINDOW,
    TRUSTED_SESSION_TTL,
)
from row_bot.access.store import AccessStore
from row_bot.access.tokens import hash_secret


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
ORIGIN = "https://row-bot.example"


def _claim(
    service: AccessService,
    *,
    lifetime: SessionLifetime = SessionLifetime.TRUSTED,
    created_at: datetime = NOW,
):
    created = service.create_invitation(
        intended_origin=ORIGIN,
        session_lifetime=lifetime,
        now=created_at,
    )
    return service.claim_invitation(
        created.token,
        intended_origin=ORIGIN,
        display_name="Renewal test browser",
        now=created_at,
    )


def _set_expiry(store: AccessStore, session_id: str, expiry: datetime) -> None:
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE access_sessions SET expires_at = ? WHERE id = ?",
            (expiry.isoformat(), session_id),
        )


def test_trusted_session_renews_only_at_or_inside_final_seven_days(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    early = _claim(service)
    _set_expiry(
        service.store,
        early.session.id,
        NOW + TRUSTED_SESSION_RENEWAL_WINDOW + timedelta(seconds=1),
    )

    early_result = service.refresh_trusted_session(early.session.id, now=NOW)

    assert early_result.session is not None
    assert early_result.renewed is False
    assert early_result.session.expires_at == (
        NOW + TRUSTED_SESSION_RENEWAL_WINDOW + timedelta(seconds=1)
    )

    for offset in (TRUSTED_SESSION_RENEWAL_WINDOW, timedelta(hours=1)):
        claim = _claim(service)
        _set_expiry(service.store, claim.session.id, NOW + offset)

        result = service.refresh_trusted_session(claim.session.id, now=NOW)

        assert result.session is not None
        assert result.renewed is True
        assert result.session.expires_at == NOW + TRUSTED_SESSION_TTL


def test_repeated_refresh_extends_once_then_becomes_a_noop(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    claim = _claim(service)
    _set_expiry(service.store, claim.session.id, NOW + timedelta(days=1))

    first = service.refresh_trusted_session(claim.session.id, now=NOW)
    second = service.refresh_trusted_session(claim.session.id, now=NOW)

    assert first.renewed is True
    assert second.renewed is False
    assert first.session == second.session


def test_temporary_and_migrated_sessions_are_active_but_never_renewed(tmp_path) -> None:
    store = AccessStore(tmp_path / "mobile.db")
    service = AccessService(store)
    temporary = _claim(
        service,
        lifetime=SessionLifetime.TEMPORARY,
        created_at=NOW,
    )
    temporary_result = service.refresh_trusted_session(
        temporary.session.id,
        now=NOW + timedelta(hours=11),
    )

    verifier = hash_secret("reviewed-legacy-refresh-secret")
    device = store.create_device_record(
        AccessDevice(
            id="legacy-device",
            display_name="Migrated browser",
            created_at=NOW,
            last_seen_at=NOW,
            revoked_at=None,
            user_agent=None,
            paired_from=None,
            access_route=None,
            legacy_source_id="legacy-device",
        )
    )
    migrated = store.create_session_record(
        AccessSession(
            id="legacy-session",
            device_id=device.id,
            token_hash=verifier.secret_hash,
            token_salt=verifier.salt,
            token_format=TokenFormat.LEGACY_RBD,
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(days=1),
            revoked_at=None,
            lifetime=SessionLifetime.MIGRATED,
            replaced_by_session_id=None,
        )
    )
    migrated_result = service.refresh_trusted_session(migrated.id, now=NOW)

    assert temporary_result.session is not None
    assert temporary_result.session.lifetime is SessionLifetime.TEMPORARY
    assert temporary_result.renewed is False
    assert migrated_result.session is not None
    assert migrated_result.session.lifetime is SessionLifetime.MIGRATED
    assert migrated_result.renewed is False


def test_revoked_expired_missing_and_device_revoked_sessions_stay_inactive(
    tmp_path,
) -> None:
    store = AccessStore(tmp_path / "mobile.db")
    service = AccessService(store)

    revoked = _claim(service)
    assert service.revoke_session(revoked.session.id, now=NOW) is True

    expired = _claim(service)
    _set_expiry(store, expired.session.id, NOW)

    device_revoked = _claim(service)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE access_devices SET revoked_at = ? WHERE id = ?",
            (NOW.isoformat(), device_revoked.device.id),
        )

    for session_id in (
        revoked.session.id,
        expired.session.id,
        device_revoked.session.id,
        "missing-session",
    ):
        result = service.refresh_trusted_session(session_id, now=NOW)
        assert result.session is None
        assert result.renewed is False


def test_renewal_persists_across_service_reopen_without_secret_material(
    tmp_path,
) -> None:
    store = AccessStore(tmp_path / "mobile.db")
    service = AccessService(store)
    claim = _claim(service)
    _set_expiry(store, claim.session.id, NOW + timedelta(days=1))

    result = service.refresh_trusted_session(claim.session.id, now=NOW)
    reopened = AccessService(AccessStore(store.db_path)).refresh_trusted_session(
        claim.session.id,
        now=NOW,
    )
    serialized = repr(result)

    assert result.renewed is True
    assert reopened.renewed is False
    assert reopened.session == result.session
    assert claim.session_token not in serialized
    assert claim.session.token_hash not in serialized
    assert claim.session.token_salt not in serialized
    assert "token_hash" not in serialized
    assert "token_salt" not in serialized
    with sqlite3.connect(store.db_path) as connection:
        audit_rows = connection.execute(
            "SELECT event_type, detail_json FROM access_events"
        ).fetchall()
    assert all(claim.session_token not in repr(row) for row in audit_rows)
    assert all(claim.session.token_hash not in repr(row) for row in audit_rows)
    assert all(claim.session.token_salt not in repr(row) for row in audit_rows)


def test_authenticated_page_installs_one_bounded_same_origin_refresh_trigger() -> None:
    source = Path("src/row_bot/app.py").read_text(encoding="utf-8")

    assert "AuthenticationKind.SESSION" in source
    assert "window.__rowBotSessionRefreshInstalled" in source
    assert "'/api/access/session/refresh'" in source
    assert "credentials: 'same-origin'" in source
    assert "SESSION_REFRESH_POLL_INTERVAL" in source
    assert "response.status === 401 || response.status === 403" in source
