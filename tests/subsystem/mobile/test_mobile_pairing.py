from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from starlette.testclient import TestClient

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.routes import register_access_routes
from row_bot.access.service import AccessService
from row_bot.mobile.routes import register_mobile_routes
from row_bot.mobile.store import MobileAuthStore

ORIGIN = "http://localhost:8080"


def _app(tmp_path):
    app = FastAPI()
    store = MobileAuthStore(tmp_path / "mobile.db")
    service = AccessService(store.access_store)
    config = AccessConfig.build(
        deployment_mode="desktop",
        allowed_hosts=("localhost",),
    )
    registration = register_access_routes(app, service=service, config=config)
    register_mobile_routes(app, store=store)
    app.add_middleware(
        AccessMiddleware,
        config=config,
        session_authenticator=registration.authenticator,
    )
    return app, service, registration


def _client(app, address: str = "127.0.0.1") -> TestClient:
    return TestClient(
        app,
        base_url=ORIGIN,
        client=(address, 50000),
        follow_redirects=False,
    )


def _start_invitation(client: TestClient) -> tuple[str, dict]:
    response = client.post(
        "/api/mobile/pair/start",
        json={"intended_origin": ORIGIN, "access_mode": "lan"},
        headers={"origin": ORIGIN},
    )
    assert response.status_code == 200
    pairing = response.json()["pairing"]
    token = parse_qs(urlsplit(pairing["pairing_url"]).query)["invitation"][0]
    return token, pairing


def test_pair_start_creates_companion_invitation_without_duplicate_secret(
    tmp_path,
) -> None:
    app, service, _registration = _app(tmp_path)
    desktop = _client(app)

    token, pairing = _start_invitation(desktop)

    assert token.startswith("rbi_")
    assert pairing["pairing_url"] == f"{ORIGIN}/connect?invitation={token}"
    assert "code" not in pairing
    assert pairing["access_mode"] == "lan"
    assert service.inspect_invitation(token).invitation.profile.value == "companion"


def test_legacy_pairing_page_redirects_to_neutral_connect_flow(tmp_path) -> None:
    app, _service, _registration = _app(tmp_path)
    phone = _client(app, "192.168.1.25")

    response = phone.get("/mobile/pair")

    assert response.status_code == 303
    assert response.headers["location"] == "/connect"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-robots-tag"] == "noindex"


def test_legacy_rbp_link_is_converted_without_claiming(tmp_path) -> None:
    app, _service, _registration = _app(tmp_path)
    phone = _client(app, "192.168.1.25")

    response = phone.get(
        "/mobile/pair",
        params={"code": "rbp_ticket.secret", "next": "/chat?thread=one"},
    )

    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert urlsplit(location).path == "/connect"
    assert query["invitation"] == ["rbi_ticket.secret"]
    assert query["next"] == ["/chat?thread=one"]


def test_legacy_json_confirm_never_claims_or_sets_a_cookie(tmp_path) -> None:
    app, service, registration = _app(tmp_path)
    desktop = _client(app)
    phone = _client(app, "192.168.1.25")
    token, _pairing = _start_invitation(desktop)

    response = phone.post(
        "/api/mobile/pair/confirm",
        json={"code": token, "display_name": "Phone"},
        headers={"origin": ORIGIN},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "connect_flow_required"
    assert response.json()["connect_url"].startswith("/connect?invitation=")
    assert response.cookies.get(registration.cookies.names.http) is None
    assert service.inspect_invitation(token).status == "available"


def test_legacy_form_confirm_redirects_without_claiming(tmp_path) -> None:
    app, service, registration = _app(tmp_path)
    desktop = _client(app)
    phone = _client(app, "192.168.1.25")
    token, _pairing = _start_invitation(desktop)

    response = phone.post(
        "/api/mobile/pair/confirm",
        data={"code": token, "display_name": "Phone"},
        headers={"origin": ORIGIN},
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/connect?invitation=")
    assert response.cookies.get(registration.cookies.names.http) is None
    assert service.inspect_invitation(token).status == "available"


def test_pair_start_rejects_forwarded_localhost_bypass(tmp_path) -> None:
    app, _service, _registration = _app(tmp_path)
    client = _client(app)

    response = client.post(
        "/api/mobile/pair/start",
        json={"intended_origin": "https://evil.example"},
        headers={
            "origin": ORIGIN,
            "x-forwarded-for": "203.0.113.4",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "untrusted_forwarding_headers"


def test_unauthenticated_mobile_session_is_neutral(tmp_path) -> None:
    app, _service, _registration = _app(tmp_path)
    remote = _client(app, "192.168.1.25")

    response = remote.get("/api/mobile/session")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "authenticated": False,
        "device": None,
    }
