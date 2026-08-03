from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from starlette.testclient import TestClient

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.models import SessionLifetime
from row_bot.access.routes import register_access_routes
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore


def _application(
    tmp_path,
    *,
    config: AccessConfig | None = None,
    base_url: str = "http://localhost:8080",
):
    selected_config = config or AccessConfig.build(
        deployment_mode="server",
        allowed_hosts=("localhost",),
    )
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    app = FastAPI()
    registration = register_access_routes(
        app,
        service=service,
        config=selected_config,
    )
    app.add_middleware(
        AccessMiddleware,
        config=selected_config,
        session_authenticator=registration.authenticator,
    )
    client = TestClient(
        app,
        base_url=base_url,
        client=("127.0.0.1", 51000),
        follow_redirects=False,
    )
    return app, client, service, registration


def _invitation(
    service: AccessService,
    *,
    origin: str = "http://localhost:8080",
    lifetime: SessionLifetime = SessionLifetime.TRUSTED,
    next_path: str = "/",
    now=None,
):
    return service.create_invitation(
        intended_origin=origin,
        session_lifetime=lifetime,
        next_path=next_path,
        now=now,
    )


def test_neutral_connect_page_reveals_no_instance_details(tmp_path) -> None:
    _app, client, _service, _registration = _application(tmp_path)

    response = client.get("/connect")

    assert response.status_code == 200
    assert "Connect to this Row-Bot" in response.text
    assert "requires approval from its owner" in response.text
    assert "row-bot access invite" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.headers["referrer-policy"] == "same-origin"


def test_get_is_informational_and_strips_ticket_from_visible_history(tmp_path) -> None:
    _app, client, service, _registration = _application(tmp_path)
    created = _invitation(service)

    response = client.get(
        "/connect",
        params={"invitation": created.token, "next": "/settings?tab=system"},
        headers={"user-agent": "LinkPreview/1.0"},
    )

    assert response.status_code == 200
    assert "Access: Full owner access" in response.text
    assert "Duration: 30 days" in response.text
    assert "history.replaceState" in response.text
    assert "searchParams.delete('invitation')" in response.text
    assert 'action="/api/access/invitations/claim"' in response.text
    assert service.inspect_invitation(created.token).status == "available"


def test_explicit_json_post_claims_once_and_issues_a_session_cookie(tmp_path) -> None:
    _app, client, service, registration = _application(tmp_path)
    created = _invitation(service)

    response = client.post(
        "/api/access/invitations/claim",
        json={
            "invitation": created.token,
            "display_name": "Workstation",
            "next": "/settings",
        },
        headers={"origin": "http://localhost:8080"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert "profile" not in payload["device"]
    assert payload["next"] == "/settings"
    assert created.token not in response.text
    cookie = response.cookies.get(registration.cookies.names.http)
    assert cookie
    authenticated = service.validate_session(cookie, touch=False)
    assert authenticated is not None
    assert authenticated.device.display_name == "Workstation"
    assert service.inspect_invitation(created.token).status == "already_claimed"


def test_compact_invitation_changes_layout_only_and_keeps_lifetime(tmp_path) -> None:
    _app, client, service, _registration = _application(tmp_path)
    created = _invitation(
        service,
        lifetime=SessionLifetime.TEMPORARY,
        next_path="/?mobile=1",
    )
    query = parse_qs(urlsplit(created.invitation_url()).query)
    assert query["next"] == ["/?mobile=1"]

    response = client.post(
        "/api/access/invitations/claim",
        json={
            "invitation": created.token,
            "display_name": "Phone",
            "next": "/?mobile=1",
        },
        headers={"origin": "http://localhost:8080"},
    )

    assert response.status_code == 200
    assert "profile" not in response.json()["device"]
    assert response.json()["next"] == "/?mobile=1"
    assert response.json()["session"]["lifetime"] == "temporary"


def test_claim_exact_origin_is_derived_from_trusted_proxy_context(tmp_path) -> None:
    config = AccessConfig.build(
        deployment_mode="server",
        trusted_proxy_cidrs=("127.0.0.0/8",),
        allowed_hosts=("rowbot.example.com",),
        public_origins=("https://rowbot.example.com",),
    )
    _app, client, service, registration = _application(
        tmp_path,
        config=config,
        base_url="http://rowbot.example.com",
    )
    created = _invitation(service, origin="https://rowbot.example.com")
    proxy_headers = {
        "host": "rowbot.example.com",
        "forwarded": ("for=192.0.2.20;proto=https;host=rowbot.example.com"),
        "origin": "https://rowbot.example.com",
    }

    response = client.post(
        "/api/access/invitations/claim",
        json={"invitation": created.token, "display_name": "Proxy browser"},
        headers=proxy_headers,
    )

    assert response.status_code == 200
    cookie_header = response.headers["set-cookie"]
    assert cookie_header.startswith(registration.cookies.names.https)
    assert "Secure" in cookie_header


def test_untrusted_forwarded_origin_cannot_satisfy_invitation_binding(tmp_path) -> None:
    _app, client, service, _registration = _application(tmp_path)
    created = _invitation(service, origin="https://rowbot.example.com")

    response = client.post(
        "/api/access/invitations/claim",
        json={"invitation": created.token},
        headers={
            "origin": "https://rowbot.example.com",
            "x-forwarded-for": "192.0.2.20",
            "x-forwarded-host": "rowbot.example.com",
            "x-forwarded-proto": "https",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "untrusted_forwarding_headers"
    assert service.inspect_invitation(created.token).status == "available"


def test_form_claim_redirects_to_clean_safe_relative_path(tmp_path) -> None:
    _app, client, service, _registration = _application(tmp_path)
    created = _invitation(service)

    response = client.post(
        "/api/access/invitations/claim",
        data={
            "invitation": created.token,
            "display_name": "Browser",
            "next": "https://attacker.example/steal",
        },
        headers={"origin": "http://localhost:8080"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert created.token not in response.headers["location"]


def test_terminal_invitation_states_render_recovery_without_claim_form(
    tmp_path,
) -> None:
    _app, client, service, _registration = _application(tmp_path)
    cancelled = _invitation(service)
    service.cancel_invitation(cancelled.invitation.id)
    expired = _invitation(
        service,
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    used = _invitation(service)
    service.claim_invitation(
        used.token,
        intended_origin="http://localhost:8080",
        display_name="First",
    )

    cases = [
        (cancelled.token, 410, "cancelled"),
        (expired.token, 410, "expired"),
        (used.token, 409, "already used"),
        ("not-a-valid-token", 400, "invalid"),
    ]
    for token, status, phrase in cases:
        response = client.get("/connect", params={"invitation": token})
        assert response.status_code == status
        assert phrase in response.text.lower()
        assert 'action="/api/access/invitations/claim"' not in response.text
        assert token not in response.text


def test_two_concurrent_post_claims_create_exactly_one_session(tmp_path) -> None:
    app, _client, service, _registration = _application(tmp_path)
    created = _invitation(service)

    def claim(name: str) -> int:
        with TestClient(
            app,
            base_url="http://localhost:8080",
            client=("127.0.0.1", 51000),
        ) as concurrent_client:
            return concurrent_client.post(
                "/api/access/invitations/claim",
                json={"invitation": created.token, "display_name": name},
                headers={"origin": "http://localhost:8080"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(claim, ("One", "Two")))

    assert statuses == [200, 409]
    assert len(service.list_sessions()) == 1
    assert len(service.list_devices()) == 1


def test_secret_values_are_absent_from_failure_logs_and_responses(
    tmp_path,
    caplog,
) -> None:
    _app, client, service, _registration = _application(tmp_path)
    created = _invitation(service)

    with caplog.at_level(logging.WARNING):
        response = client.post(
            "/api/access/invitations/claim",
            json={"invitation": f"{created.token}wrong"},
            headers={"origin": "http://localhost:8080"},
        )

    assert response.status_code == 400
    assert created.token not in response.text
    assert created.token not in caplog.text
