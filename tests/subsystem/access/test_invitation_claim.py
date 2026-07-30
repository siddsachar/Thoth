from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from row_bot.access.models import SessionLifetime
from row_bot.access.service import AccessService
from row_bot.access.store import (
    AccessStore,
    CLAIM_FAILURE_LIMIT,
    InvitationClaimError,
)


NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
ORIGIN = "https://row-bot.example"


def _service(tmp_path) -> AccessService:
    return AccessService(AccessStore(tmp_path / "mobile.db"))


def test_inspection_does_not_claim_and_success_uses_immutable_grant(tmp_path) -> None:
    service = _service(tmp_path)
    created = service.create_invitation(
        intended_origin="HTTPS://ROW-BOT.EXAMPLE:443/",
        session_lifetime=SessionLifetime.TEMPORARY,
        now=NOW,
    )

    first = service.inspect_invitation(created.token, now=NOW)
    second = service.inspect_invitation(created.token, now=NOW)
    assert first.status == second.status == "available"
    assert first.invitation.claimed_at is None

    claim = service.claim_invitation(
        created.token,
        intended_origin=ORIGIN,
        display_name="Desktop browser",
        session_lifetime=SessionLifetime.TEMPORARY,
        now=NOW,
    )

    assert claim.session_lifetime is SessionLifetime.TEMPORARY
    assert claim.intended_origin == ORIGIN
    assert claim.session.expires_at == NOW + timedelta(hours=12)
    assert (
        service.inspect_invitation(created.token, now=NOW).status == "already_claimed"
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"intended_origin": "https://other.example"}, "origin_mismatch"),
        (
            {
                "intended_origin": ORIGIN,
                "session_lifetime": SessionLifetime.TEMPORARY,
            },
            "immutable_mismatch",
        ),
    ],
)
def test_claim_cannot_override_origin_or_lifetime(
    tmp_path,
    kwargs,
    reason,
) -> None:
    service = _service(tmp_path)
    created = service.create_invitation(
        intended_origin=ORIGIN,
        session_lifetime=SessionLifetime.TRUSTED,
        now=NOW,
    )

    with pytest.raises(InvitationClaimError) as caught:
        service.claim_invitation(
            created.token,
            display_name="Attacker",
            now=NOW,
            **kwargs,
        )
    assert caught.value.reason == reason
    assert service.inspect_invitation(created.token, now=NOW).status == "available"
    assert service.list_devices() == []


def test_concurrent_claim_creates_exactly_one_device_and_session(tmp_path) -> None:
    service = _service(tmp_path)
    created = service.create_invitation(
        intended_origin=ORIGIN,
        now=NOW,
    )

    def claim(index: int) -> str:
        try:
            service.claim_invitation(
                created.token,
                intended_origin=ORIGIN,
                display_name=f"Browser {index}",
                now=NOW,
            )
            return "success"
        except InvitationClaimError as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(claim, range(6)))

    assert results.count("success") == 1
    assert results.count("already_claimed") == 5
    assert len(service.list_devices()) == 1
    assert len(service.list_sessions()) == 1


def test_expired_cancelled_and_locked_invitations_are_terminal(tmp_path) -> None:
    service = _service(tmp_path)
    expired = service.create_invitation(
        intended_origin=ORIGIN,
        now=NOW,
    )
    with pytest.raises(InvitationClaimError) as expired_error:
        service.claim_invitation(
            expired.token,
            intended_origin=ORIGIN,
            display_name="Late",
            now=NOW + timedelta(minutes=10),
        )
    assert expired_error.value.reason == "expired"

    cancelled = service.create_invitation(
        intended_origin=ORIGIN,
        now=NOW,
    )
    assert service.cancel_invitation(cancelled.invitation.id, now=NOW) is True
    with pytest.raises(InvitationClaimError) as cancelled_error:
        service.claim_invitation(
            cancelled.token,
            intended_origin=ORIGIN,
            display_name="Cancelled",
            now=NOW,
        )
    assert cancelled_error.value.reason == "cancelled"

    locked = service.create_invitation(
        intended_origin=ORIGIN,
        now=NOW,
    )
    tampered = f"{locked.token[:-1]}{'A' if locked.token[-1] != 'A' else 'B'}"
    for _index in range(CLAIM_FAILURE_LIMIT):
        with pytest.raises(InvitationClaimError) as invalid_error:
            service.claim_invitation(
                tampered,
                intended_origin=ORIGIN,
                display_name="Guess",
                now=NOW,
            )
        assert invalid_error.value.reason == "invalid_invitation"
    with pytest.raises(InvitationClaimError) as locked_error:
        service.claim_invitation(
            locked.token,
            intended_origin=ORIGIN,
            display_name="Real",
            now=NOW,
        )
    assert locked_error.value.reason == "locked"


def test_claim_failure_lock_is_scoped_to_effective_client(tmp_path) -> None:
    service = _service(tmp_path)
    invitation = service.create_invitation(
        intended_origin=ORIGIN,
        now=NOW,
    )
    tampered = f"{invitation.token[:-1]}{'A' if invitation.token[-1] != 'A' else 'B'}"

    for _index in range(CLAIM_FAILURE_LIMIT):
        with pytest.raises(InvitationClaimError) as invalid_error:
            service.claim_invitation(
                tampered,
                intended_origin=ORIGIN,
                display_name="Guess",
                effective_client="192.0.2.10",
                now=NOW,
            )
        assert invalid_error.value.reason == "invalid_invitation"

    assert (
        service.inspect_invitation(
            invitation.token,
            effective_client="192.0.2.10",
            now=NOW,
        ).status
        == "locked"
    )
    assert (
        service.inspect_invitation(
            invitation.token,
            effective_client="192.0.2.11",
            now=NOW,
        ).status
        == "available"
    )
    claim = service.claim_invitation(
        invitation.token,
        intended_origin=ORIGIN,
        display_name="Real browser",
        effective_client="192.0.2.11",
        now=NOW,
    )
    assert claim.device.display_name == "Real browser"


def test_raw_invitation_and_session_tokens_never_reach_database(
    tmp_path, caplog
) -> None:
    service = _service(tmp_path)
    created = service.create_invitation(
        intended_origin=ORIGIN,
        now=NOW,
    )
    claim = service.claim_invitation(
        created.token,
        intended_origin=ORIGIN,
        display_name="Workstation",
        now=NOW,
    )

    with sqlite3.connect(service.store.db_path) as connection:
        dump = "\n".join(connection.iterdump())
    assert created.token not in dump
    assert claim.session_token not in dump
    assert created.token not in caplog.text
    assert claim.session_token not in caplog.text
    assert service.validate_session(claim.session_token, now=NOW) is not None
