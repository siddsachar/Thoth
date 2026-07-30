from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from row_bot.mobile import auth
from row_bot.mobile.store import MobileAuthStore


def _store(tmp_path) -> MobileAuthStore:
    return MobileAuthStore(tmp_path / "mobile.db")


def test_pairing_and_device_tokens_are_hashed_at_rest(tmp_path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)

    ticket = auth.create_pairing_ticket(store, intended_origin="https://rowbot.test", now=now)
    code_id, code_secret = auth.parse_pairing_code(ticket.code) or ("", "")
    pairing_row = store.get_pairing_code(code_id)

    assert pairing_row is not None
    assert code_secret
    assert code_secret not in pairing_row.code_hash
    assert code_secret not in pairing_row.code_salt
    assert ticket.code not in pairing_row.code_hash

    confirmation = auth.confirm_pairing(
        store,
        code=ticket.code,
        display_name="Pixel",
        user_agent="pytest",
        paired_from="127.0.0.1",
        now=now + timedelta(seconds=1),
    )
    device_id, token_secret = auth.parse_device_token(confirmation.token) or ("", "")
    device = store.get_device(device_id)

    assert device is not None
    assert device.display_name == "Pixel"
    assert token_secret
    assert token_secret not in device.token_hash
    assert token_secret not in device.token_salt
    assert confirmation.token not in device.token_hash

    with sqlite3.connect(store.db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert ticket.code not in dump
    assert confirmation.token not in dump


def test_expired_pairing_code_fails_without_creating_device(tmp_path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)
    ticket = auth.create_pairing_ticket(
        store,
        ttl=timedelta(seconds=1),
        now=now,
    )

    try:
        auth.confirm_pairing(
            store,
            code=ticket.code,
            display_name="Late phone",
            now=now + timedelta(seconds=2),
        )
    except auth.PairingError as exc:
        assert exc.reason == "expired"
    else:
        raise AssertionError("expired pairing code unexpectedly succeeded")

    assert store.list_devices() == []


def test_pairing_code_is_single_use(tmp_path) -> None:
    store = _store(tmp_path)
    ticket = auth.create_pairing_ticket(store)

    first = auth.confirm_pairing(store, code=ticket.code, display_name="Phone")
    assert first.device.id

    try:
        auth.confirm_pairing(store, code=ticket.code, display_name="Replay")
    except auth.PairingError as exc:
        assert exc.reason == "already_claimed"
    else:
        raise AssertionError("reused pairing code unexpectedly succeeded")

    assert len(store.list_devices()) == 1


def test_bad_pairing_code_locks_only_the_failing_client(tmp_path) -> None:
    store = _store(tmp_path)
    ticket = auth.create_pairing_ticket(store)
    code_id, secret = auth.parse_pairing_code(ticket.code) or ("", "")
    replacement = "A" if secret[-1] != "A" else "B"
    tampered = f"rbp_{code_id}.{secret[:-1]}{replacement}"

    for _ in range(auth.PAIRING_FAILURE_LIMIT):
        try:
            auth.confirm_pairing(store, code=tampered, display_name="Bad phone")
        except auth.PairingError as exc:
            assert exc.reason == "invalid_code"
        else:
            raise AssertionError("tampered pairing code unexpectedly succeeded")

    locked = store.get_pairing_code(code_id)
    assert locked is not None
    assert locked.failed_attempts == auth.PAIRING_FAILURE_LIMIT
    assert locked.locked_until is None
    assert store.access_store.invitation_failure_locked(
        code_id,
        effective_client=None,
    )

    try:
        auth.confirm_pairing(store, code=ticket.code, display_name="Real phone")
    except auth.PairingError as exc:
        assert exc.reason == "locked"
    else:
        raise AssertionError("locked pairing code unexpectedly succeeded")

    confirmation = auth.confirm_pairing(
        store,
        code=ticket.code,
        display_name="Real phone",
        paired_from="192.0.2.20",
    )
    assert confirmation.device.display_name == "Real phone"


def test_revoked_device_stops_validating(tmp_path) -> None:
    store = _store(tmp_path)
    ticket = auth.create_pairing_ticket(store)
    confirmation = auth.confirm_pairing(store, code=ticket.code, display_name="Phone")

    validated = auth.validate_device_token(store, confirmation.token)
    assert validated is not None
    assert validated.id == confirmation.device.id
    assert validated.last_seen_at is not None

    assert store.revoke_device(confirmation.device.id) is True
    assert auth.validate_device_token(store, confirmation.token) is None


def test_access_events_are_display_safe(tmp_path) -> None:
    store = _store(tmp_path)

    event = store.log_event(
        "paired",
        device_id="device-1",
        ip="127.0.0.1",
        user_agent="pytest",
        detail={"access_mode": "localhost"},
    )

    assert store.recent_events()[0].id == event.id
    assert store.recent_events()[0].to_public_dict()["detail"] == {"access_mode": "localhost"}
