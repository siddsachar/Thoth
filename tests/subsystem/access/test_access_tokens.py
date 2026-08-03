from __future__ import annotations

from row_bot.access.tokens import (
    issue_invitation_token,
    issue_session_token,
    parse_invitation_token,
    parse_session_token,
    verify_secret,
)


def test_new_tokens_have_256_bit_secrets_and_versioned_prefixes() -> None:
    invitation = issue_invitation_token("a" * 32)
    session = issue_session_token("b" * 32)

    invitation_parts = parse_invitation_token(invitation.token)
    session_parts = parse_session_token(session.token)

    assert invitation_parts is not None
    assert session_parts is not None
    assert len(invitation_parts[1]) >= 40
    assert len(session_parts[1]) >= 40
    assert invitation.token.startswith("rbi_")
    assert session.token.startswith("rbs_")
    assert verify_secret(
        invitation_parts[1],
        salt=invitation.secret_salt,
        expected_hash=invitation.secret_hash,
    )
    assert not verify_secret(
        f"{invitation_parts[1]}x",
        salt=invitation.secret_salt,
        expected_hash=invitation.secret_hash,
    )


def test_malformed_tokens_fail_closed() -> None:
    assert parse_invitation_token("") is None
    assert parse_invitation_token("rbi_not-an-id.short") is None
    assert parse_invitation_token("rbs_" + "a" * 32 + "." + "x" * 43) is None
    assert parse_session_token("rbi_" + "a" * 32 + "." + "x" * 43) is None
