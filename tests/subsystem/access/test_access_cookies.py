from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.responses import Response

from row_bot.access.cookies import (
    AccessCookieManager,
    LEGACY_HTTP_COOKIE_NAME,
    LEGACY_HTTPS_COOKIE_NAME,
    cookie_names_for_instance,
)


def _set_cookie_headers(response: Response) -> list[str]:
    return [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    ]


def test_cookie_names_are_stable_and_isolated_per_instance() -> None:
    first = cookie_names_for_instance("instance-a")
    same = cookie_names_for_instance("instance-a")
    second = cookie_names_for_instance("instance-b")

    assert first == same
    assert first != second
    assert first.https.startswith("__Host-row_bot_access_")
    assert first.http.startswith("row_bot_access_")


def test_https_cookie_uses_host_prefix_and_secure_attributes() -> None:
    manager = AccessCookieManager("instance-a")
    response = Response()
    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)

    manager.set_session(
        response,
        "session-token",
        context="https",
        expires_at=now + timedelta(hours=12),
        now=now,
    )

    header = _set_cookie_headers(response)[0]
    assert header.startswith(f"{manager.names.https}=session-token;")
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=strict" in header
    assert "Path=/" in header
    assert "Max-Age=43200" in header
    assert "Domain=" not in header


def test_http_cookie_is_instance_scoped_without_secure_flag() -> None:
    manager = AccessCookieManager("instance-a")
    response = Response()
    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)

    manager.set_session(
        response,
        "session-token",
        context="http",
        expires_at=now + timedelta(days=30),
        now=now,
    )

    header = _set_cookie_headers(response)[0]
    assert header.startswith(f"{manager.names.http}=session-token;")
    assert "HttpOnly" in header
    assert "Secure" not in header
    assert "SameSite=strict" in header
    assert "Max-Age=2592000" in header


def test_cookie_max_age_never_outlives_server_expiry() -> None:
    manager = AccessCookieManager("instance-a")
    response = Response()
    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)

    manager.set_session(
        response,
        "expired-token",
        context="http",
        expires_at=now - timedelta(seconds=1),
        now=now,
    )

    assert "Max-Age=0" in _set_cookie_headers(response)[0]


def test_extract_uses_only_the_current_instance_namespace() -> None:
    first = AccessCookieManager("instance-a")
    second = AccessCookieManager("instance-b")
    header = (
        f"{first.names.http}=first-token; "
        f"{second.names.http}=second-token; unrelated=value"
    )

    assert first.extract(header, context="http") == "first-token"
    assert second.extract(header, context="http") == "second-token"
    assert first.extract("malformed cookie = ;", context="http") == ""


def test_clear_removes_current_and_legacy_cookie_names() -> None:
    manager = AccessCookieManager("instance-a")
    response = Response()

    manager.clear(response)

    headers = _set_cookie_headers(response)
    assert len(headers) == 4
    assert any(header.startswith(f"{manager.names.https}=") for header in headers)
    assert any(header.startswith(f"{manager.names.http}=") for header in headers)
    assert any(header.startswith(f"{LEGACY_HTTPS_COOKIE_NAME}=") for header in headers)
    assert any(header.startswith(f"{LEGACY_HTTP_COOKIE_NAME}=") for header in headers)
    https_headers = [header for header in headers if header.startswith("__Host-")]
    assert all("Secure" in header for header in https_headers)
