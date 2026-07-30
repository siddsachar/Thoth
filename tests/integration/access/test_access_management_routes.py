from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from starlette.testclient import TestClient

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.routes import register_access_routes
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore


def _application(tmp_path, *, mode: str = "server"):
    config = AccessConfig.build(
        deployment_mode=mode,
        allowed_hosts=("localhost",),
    )
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    app = FastAPI()
    registration = register_access_routes(app, service=service, config=config)
    app.add_middleware(
        AccessMiddleware,
        config=config,
        session_authenticator=registration.authenticator,
    )
    client = TestClient(
        app,
        base_url="http://localhost:8080",
        client=("127.0.0.1", 51000),
        follow_redirects=False,
    )
    return client, service, registration


def _session_cookie(
    service: AccessService,
    registration,
    *,
    name: str,
) -> tuple[str, str, str]:
    created = service.create_invitation(
        intended_origin="http://localhost:8080",
    )
    claimed = service.claim_invitation(
        created.token,
        intended_origin="http://localhost:8080",
        display_name=name,
    )
    cookie = f"{registration.cookies.names.http}={claimed.session_token}"
    return cookie, claimed.device.id, claimed.session.id


def test_local_desktop_owner_can_create_desktop_invitation(tmp_path) -> None:
    client, _service, _registration = _application(tmp_path, mode="desktop")

    response = client.post(
        "/api/access/invitations",
        json={
            "layout": "desktop",
            "session_lifetime": "trusted",
            "origin": "http://localhost:8080",
        },
        headers={"origin": "http://localhost:8080"},
    )

    assert response.status_code == 201
    assert response.json()["invitation"]["layout"] == "desktop"
    assert "profile" not in response.json()["invitation"]
    assert response.json()["invitation"]["session_lifetime"] == "trusted"
    assert "token" not in response.json()
    assert parse_qs(urlsplit(response.json()["invitation_url"]).query)["invitation"]


def test_remote_owner_can_list_status_invitations_devices_and_sessions(
    tmp_path,
) -> None:
    client, service, registration = _application(tmp_path)
    owner_cookie, owner_device_id, owner_session_id = _session_cookie(
        service,
        registration,
        name="Owner",
    )
    headers = {"cookie": owner_cookie}

    status = client.get("/api/access/status", headers=headers)
    invitations = client.get("/api/access/invitations", headers=headers)
    devices = client.get("/api/access/devices", headers=headers)

    assert status.status_code == 200
    assert status.json()["devices"] == 1
    assert status.json()["sessions"] == 1
    assert invitations.status_code == 200
    assert devices.status_code == 200
    device = devices.json()["devices"][0]
    assert device["id"] == owner_device_id
    assert device["sessions"][0]["id"] == owner_session_id
    assert "token_hash" not in devices.text
    assert "token_salt" not in devices.text


def test_phone_owner_can_use_access_management_routes(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    phone_cookie, device_id, _session_id = _session_cookie(
        service,
        registration,
        name="Phone",
    )
    safe_headers = {"cookie": phone_cookie}
    unsafe_headers = {
        **safe_headers,
        "origin": "http://localhost:8080",
    }

    responses = [
        client.get("/api/access/status", headers=safe_headers),
        client.get("/api/access/invitations", headers=safe_headers),
        client.get("/api/access/devices", headers=safe_headers),
        client.post(
            "/api/access/invitations",
            json={"layout": "compact"},
            headers=unsafe_headers,
        ),
    ]

    assert all(response.status_code in {200, 201} for response in responses)
    assert device_id


def test_owner_creates_lists_and_cancels_invitation_without_relisting_secret(
    tmp_path,
    caplog,
) -> None:
    client, service, registration = _application(tmp_path)
    owner_cookie, _device_id, _session_id = _session_cookie(
        service,
        registration,
        name="Owner",
    )
    headers = {
        "cookie": owner_cookie,
        "origin": "http://localhost:8080",
    }

    with caplog.at_level(logging.WARNING):
        created = client.post(
            "/api/access/invitations",
            json={
                "layout": "compact",
                "session_lifetime": "temporary",
            },
            headers=headers,
        )
    token = parse_qs(urlsplit(created.json()["invitation_url"]).query)["invitation"][0]
    invitation_id = created.json()["invitation"]["id"]
    listed = client.get(
        "/api/access/invitations",
        headers={"cookie": owner_cookie},
    )
    cancelled = client.post(
        f"/api/access/invitations/{invitation_id}/cancel",
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["invitation"]["layout"] == "compact"
    assert "profile" not in created.json()["invitation"]
    assert created.json()["invitation"]["session_lifetime"] == "temporary"
    assert listed.status_code == 200
    assert token not in listed.text
    assert "secret_hash" not in listed.text
    assert "secret_salt" not in listed.text
    assert token not in caplog.text
    assert cancelled.json() == {"ok": True, "cancelled": True}
    assert service.inspect_invitation(token).status == "cancelled"


def test_owner_revokes_device_and_its_sessions_immediately(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    owner_cookie, _owner_device_id, _owner_session_id = _session_cookie(
        service,
        registration,
        name="Owner",
    )
    phone_cookie, phone_device_id, phone_session_id = _session_cookie(
        service,
        registration,
        name="Phone",
    )

    response = client.post(
        f"/api/access/devices/{phone_device_id}/revoke",
        headers={
            "cookie": owner_cookie,
            "origin": "http://localhost:8080",
        },
    )
    phone_status = client.get(
        "/api/access/session",
        headers={"cookie": phone_cookie},
    )

    assert response.json() == {"ok": True, "revoked": True}
    assert service.store.get_session(phone_session_id).revoked_at is not None
    assert phone_status.json()["authenticated"] is False


def test_self_revoke_clears_current_instance_and_legacy_cookies(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    owner_cookie, owner_device_id, _owner_session_id = _session_cookie(
        service,
        registration,
        name="Owner",
    )

    response = client.post(
        f"/api/access/devices/{owner_device_id}/revoke",
        headers={
            "cookie": owner_cookie,
            "origin": "http://localhost:8080",
        },
    )

    assert response.status_code == 200
    set_cookies = response.headers.get_list("set-cookie")
    assert len(set_cookies) == 4
    assert any(registration.cookies.names.http in value for value in set_cookies)
    assert any("__Host-row_bot_mobile" in value for value in set_cookies)


def test_management_mutations_require_exact_same_origin(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    owner_cookie, device_id, _session_id = _session_cookie(
        service,
        registration,
        name="Owner",
    )

    missing = client.post(
        "/api/access/invitations",
        json={"layout": "compact"},
        headers={"cookie": owner_cookie},
    )
    wrong = client.post(
        f"/api/access/devices/{device_id}/revoke",
        headers={
            "cookie": owner_cookie,
            "origin": "https://attacker.example",
        },
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing.json()["error"] == "origin_required"
    assert wrong.json()["error"] == "origin_required"


def test_invalid_invitation_options_fail_without_creating_records(tmp_path) -> None:
    client, service, registration = _application(tmp_path)
    owner_cookie, _device_id, _session_id = _session_cookie(
        service,
        registration,
        name="Owner",
    )
    headers = {
        "cookie": owner_cookie,
        "origin": "http://localhost:8080",
    }
    before = len(service.list_invitations())

    layout = client.post(
        "/api/access/invitations",
        json={"layout": "restricted"},
        headers=headers,
    )
    legacy_profile = client.post(
        "/api/access/invitations",
        json={"profile": "companion"},
        headers=headers,
    )
    origin = client.post(
        "/api/access/invitations",
        json={"origin": "javascript:alert(1)"},
        headers=headers,
    )

    assert layout.status_code == 400
    assert legacy_profile.status_code == 400
    assert "choose a layout" in legacy_profile.json()["detail"]
    assert origin.status_code == 400
    assert len(service.list_invitations()) == before
