from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from row_bot.access.config import AccessConfig
from row_bot.access.routes import register_access_routes
from row_bot.access.service import AccessService
from row_bot.mobile.access_gate import MobileAccessGate
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

    @app.get("/")
    async def root():
        return {"ok": True, "surface": "root"}

    @app.get("/_media/{path:path}")
    async def media(path: str):  # noqa: ARG001
        return JSONResponse({"private": "media"})

    @app.get("/published/{path:path}")
    async def published(path: str):  # noqa: ARG001
        return JSONResponse({"private": "published"})

    @app.post("/api/voice/realtime/client-secret")
    async def voice_secret():
        return {"secret": "should-not-leak"}

    registration = register_access_routes(
        app,
        service=service,
        config=config,
    )
    register_mobile_routes(app, store=store)
    app.add_middleware(MobileAccessGate, store=store, config=config)
    return app, service, registration


def _client(app, address: str, *, follow_redirects: bool = False) -> TestClient:
    return TestClient(
        app,
        base_url=ORIGIN,
        client=(address, 50000),
        follow_redirects=follow_redirects,
    )


def _create_companion_invitation(local: TestClient) -> str:
    response = local.post(
        "/api/mobile/pair/start",
        json={"intended_origin": ORIGIN},
        headers={"origin": ORIGIN},
    )
    assert response.status_code == 200
    pairing_url = response.json()["pairing"]["pairing_url"]
    invitation = parse_qs(urlsplit(pairing_url).query)["invitation"][0]
    assert invitation.startswith("rbi_")
    assert invitation not in response.text.replace(pairing_url, "")
    return invitation


def _claim(remote: TestClient, invitation: str):
    return remote.post(
        "/api/access/invitations/claim",
        json={"invitation": invitation, "display_name": "Phone"},
        headers={"origin": ORIGIN},
    )


def test_unpaired_remote_can_only_reach_minimal_connection_routes(tmp_path) -> None:
    app, _service, _registration = _app(tmp_path)
    remote = _client(app, "192.168.1.25")

    assert remote.get("/api/mobile/session").status_code == 200
    pair = remote.get("/mobile/pair")
    root = remote.get("/", headers={"accept": "text/html"})

    assert pair.status_code == 303
    assert pair.headers["location"] == "/connect"
    assert root.status_code == 303
    assert root.headers["location"].startswith("/connect?next=")
    assert remote.get("/_media/thread/file.png").status_code == 401
    assert remote.get("/published/page.html").status_code == 401
    assert remote.post("/api/voice/realtime/client-secret").status_code == 401


def test_forwarded_localhost_cannot_bypass_gate(tmp_path) -> None:
    app, _service, _registration = _app(tmp_path)
    local = _client(app, "127.0.0.1")

    response = local.get(
        "/published/page.html",
        headers={"x-forwarded-for": "203.0.113.4"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "untrusted_forwarding_headers"


def test_companion_claim_allows_private_routes_and_revoke_blocks_it(
    tmp_path,
) -> None:
    app, service, registration = _app(tmp_path)
    local = _client(app, "127.0.0.1")
    remote = _client(app, "192.168.1.25")
    invitation = _create_companion_invitation(local)

    inspection = remote.get("/connect", params={"invitation": invitation})
    claim = _claim(remote, invitation)

    assert inspection.status_code == 200
    assert claim.status_code == 200
    assert claim.json()["device"]["profile"] == "companion"
    assert claim.cookies.get(registration.cookies.names.http)
    assert remote.get("/").status_code == 200
    assert remote.get("/_media/thread/file.png").status_code == 200
    assert remote.get("/published/page.html").status_code == 200
    assert service.inspect_invitation(invitation).status == "already_claimed"

    device_id = claim.json()["device"]["id"]
    revoked = local.post(
        f"/api/mobile/devices/{device_id}/revoke",
        headers={"origin": ORIGIN},
    )
    assert revoked.status_code == 200
    assert remote.get("/published/page.html").status_code == 401


def test_legacy_confirm_endpoint_never_claims_or_sets_cookie(tmp_path) -> None:
    app, service, registration = _app(tmp_path)
    local = _client(app, "127.0.0.1")
    remote = _client(app, "192.168.1.25")
    invitation = _create_companion_invitation(local)

    response = remote.post(
        "/api/mobile/pair/confirm",
        json={"code": invitation, "display_name": "Phone"},
        headers={"origin": ORIGIN},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "connect_flow_required"
    assert response.cookies.get(registration.cookies.names.http) is None
    assert service.inspect_invitation(invitation).status == "available"
