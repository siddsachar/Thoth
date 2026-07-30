from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from starlette.testclient import TestClient

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.routes import register_access_routes
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore


def _application(tmp_path, *, port: int = 8080):
    config = AccessConfig.build(
        deployment_mode="server",
        allowed_hosts=("localhost",),
    )
    service = AccessService(AccessStore(tmp_path / f"mobile-{port}.db"))
    app = FastAPI()
    registration = register_access_routes(app, service=service, config=config)
    app.add_middleware(
        AccessMiddleware,
        config=config,
        session_authenticator=registration.authenticator,
    )
    client = TestClient(
        app,
        base_url=f"http://localhost:{port}",
        client=("127.0.0.1", 51000),
        follow_redirects=False,
    )
    return client, service, registration


def _claim_owner(client: TestClient, service: AccessService, *, port: int = 8080):
    created = service.create_invitation(
        intended_origin=f"http://localhost:{port}",
    )
    response = client.post(
        "/api/access/invitations/claim",
        json={"invitation": created.token, "display_name": "Owner browser"},
        headers={"origin": f"http://localhost:{port}"},
    )
    assert response.status_code == 200
    return response


def test_session_status_persists_across_refreshes(tmp_path) -> None:
    client, service, _registration = _application(tmp_path)
    claim = _claim_owner(client, service)

    first = client.get("/api/access/session")
    second = client.get("/api/access/session")

    assert claim.status_code == 200
    assert first.json()["authenticated"] is True
    assert "profile" not in first.json()
    assert first.json()["session_id"]
    assert second.json()["session_id"] == first.json()["session_id"]


def test_server_expiry_is_authoritative_even_if_cookie_survives(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    created = service.create_invitation(
        intended_origin="http://localhost:8080",
        now=old,
    )
    claim = service.claim_invitation(
        created.token,
        intended_origin="http://localhost:8080",
        display_name="Expired browser",
        now=old,
    )
    cookie = f"{registration.cookies.names.http}={claim.session_token}"

    response = client.get(
        "/api/access/session",
        headers={"cookie": cookie},
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_logout_revokes_session_and_clears_current_and_legacy_cookies(
    tmp_path,
) -> None:
    client, service, registration = _application(tmp_path)
    claim = _claim_owner(client, service)
    session_id = claim.json()["session"]["id"]

    response = client.post(
        "/api/access/logout",
        headers={"origin": "http://localhost:8080"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "revoked": True}
    assert service.store.get_session(session_id).revoked_at is not None
    set_cookies = response.headers.get_list("set-cookie")
    assert len(set_cookies) == 4
    assert any(registration.cookies.names.http in value for value in set_cookies)
    assert any("__Host-row_bot_mobile" in value for value in set_cookies)
    assert any("row_bot_mobile_lan" in value for value in set_cookies)
    assert client.get("/api/access/session").json()["authenticated"] is False


def test_revoked_session_cookie_cannot_be_reused(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    claim_response = _claim_owner(client, service)
    token = claim_response.cookies.get(registration.cookies.names.http)
    session_id = claim_response.json()["session"]["id"]
    assert token

    service.revoke_session(session_id)
    response = client.get(
        "/api/access/session",
        headers={"cookie": f"{registration.cookies.names.http}={token}"},
    )

    assert response.json()["authenticated"] is False


def test_two_instances_on_one_hostname_keep_sessions_independent(tmp_path) -> None:
    first, first_service, first_registration = _application(
        tmp_path / "first",
        port=8081,
    )
    second, second_service, second_registration = _application(
        tmp_path / "second",
        port=8082,
    )
    first_claim = _claim_owner(first, first_service, port=8081)
    second_claim = _claim_owner(second, second_service, port=8082)
    first_token = first_claim.cookies.get(first_registration.cookies.names.http)
    second_token = second_claim.cookies.get(second_registration.cookies.names.http)
    assert first_token and second_token
    assert first_registration.cookies.names != second_registration.cookies.names
    combined = (
        f"{first_registration.cookies.names.http}={first_token}; "
        f"{second_registration.cookies.names.http}={second_token}"
    )

    first_status = first.get(
        "/api/access/session",
        headers={"cookie": combined},
    )
    second_status = second.get(
        "/api/access/session",
        headers={"cookie": combined},
    )

    assert first_status.json()["authenticated"] is True
    assert second_status.json()["authenticated"] is True
    assert first_status.json()["session_id"] != second_status.json()["session_id"]


def test_session_status_never_returns_raw_cookie_or_token(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    claim = _claim_owner(client, service)
    token = claim.cookies.get(registration.cookies.names.http)
    assert token

    response = client.get("/api/access/session")

    assert token not in response.text
    assert "token_hash" not in response.text
    assert "token_salt" not in response.text
    assert response.headers["cache-control"] == "no-store"
