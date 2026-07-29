from __future__ import annotations

import time

from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.request_context import SessionIdentity


class FakeSessionAuthenticator:
    def __init__(self) -> None:
        self.active = {"owner-token": ("owner", "owner-session")}

    def revoke(self, token: str) -> None:
        self.active.pop(token, None)

    def authenticate_scope(self, scope, provenance):  # noqa: ARG002
        headers = {
            bytes(name).lower(): bytes(value).decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        cookie = headers.get(b"cookie", "")
        token = next(
            (
                item.split("=", 1)[1]
                for item in cookie.split(";")
                if item.strip().startswith("row_bot_test=")
            ),
            "",
        ).strip()
        result = self.active.get(token)
        if result is None:
            return None
        profile, session_id = result
        return SessionIdentity(
            profile=profile,
            device_id=f"{profile}-device",
            session_id=session_id,
        )


def _build_app(authenticator: FakeSessionAuthenticator) -> TestClient:
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"surface": "desktop"}

    @app.get("/api/private")
    async def private():
        return {"ok": True}

    @app.get("/api/access/devices")
    async def devices():
        return {"devices": []}

    @app.websocket("/_nicegui_ws/socket.io")
    async def nicegui_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive_text()
                await websocket.send_text(message)
        except WebSocketDisconnect:
            return

    middleware = AccessMiddleware(
        app,
        config=AccessConfig.build(
            deployment_mode="server",
            allowed_hosts=("localhost",),
        ),
        session_authenticator=authenticator,
        websocket_revalidation_seconds=0.05,
    )
    return TestClient(
        middleware,
        base_url="http://localhost:8080",
        client=("127.0.0.1", 51000),
        follow_redirects=False,
    )


def test_server_http_and_websocket_use_the_same_session_gate() -> None:
    authenticator = FakeSessionAuthenticator()
    client = _build_app(authenticator)
    cookie_header = {"cookie": "row_bot_test=owner-token"}

    assert client.get("/api/private").status_code == 401
    assert client.get("/api/private", headers=cookie_header).status_code == 200

    with pytest.raises(WebSocketDisconnect) as unpaired:
        with client.websocket_connect(
            "/_nicegui_ws/socket.io",
            headers={
                "host": "localhost:8080",
                "origin": "http://localhost:8080",
            },
        ):
            pass
    assert unpaired.value.code == 1008

    with client.websocket_connect(
        "/_nicegui_ws/socket.io",
        headers={
            "host": "localhost:8080",
            "origin": "http://localhost:8080",
            **cookie_header,
        },
    ) as websocket:
        websocket.send_text("connected")
        assert websocket.receive_text() == "connected"


def test_revocation_blocks_http_immediately_and_active_websocket_within_bound() -> None:
    authenticator = FakeSessionAuthenticator()
    client = _build_app(authenticator)
    cookie_header = {"cookie": "row_bot_test=owner-token"}

    with client.websocket_connect(
        "/_nicegui_ws/socket.io",
        headers={
            "host": "localhost:8080",
            "origin": "http://localhost:8080",
            **cookie_header,
        },
    ) as websocket:
        websocket.send_text("before")
        assert websocket.receive_text() == "before"

        authenticator.revoke("owner-token")
        assert client.get("/api/private", headers=cookie_header).status_code == 401

        started = time.monotonic()
        with pytest.raises(WebSocketDisconnect) as revoked:
            websocket.receive_text()
        elapsed = time.monotonic() - started

    assert revoked.value.code == 1008
    assert elapsed < 1.0


def test_companion_session_cannot_cross_owner_route_boundary() -> None:
    authenticator = FakeSessionAuthenticator()
    authenticator.active["companion-token"] = ("companion", "companion-session")
    client = _build_app(authenticator)

    response = client.get(
        "/api/access/devices",
        headers={"cookie": "row_bot_test=companion-token"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "capability_required"
