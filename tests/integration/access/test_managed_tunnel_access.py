from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from fastapi import FastAPI, Request, WebSocket
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from row_bot.access.config import AccessConfig
from row_bot.access.cookies import LEGACY_HTTPS_COOKIE_NAME
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.request_context import ACCESS_CONTEXT_SCOPE_KEY
from row_bot.access.routes import register_access_routes
from row_bot.access.runtime_policy import RuntimeAccessPolicy
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore
from row_bot.access.tokens import hash_secret
from row_bot.tunnel import TunnelManager, TunnelProvider

MANAGED_ORIGIN = "https://stable-phone.ngrok-free.app"
CHANGED_ORIGIN = "https://changed-phone.ngrok-free.app"
LEGACY_DEVICE_ID = "a" * 32
LEGACY_SECRET = "legacy-secret-that-is-long-enough"
LEGACY_TOKEN = f"rbd_{LEGACY_DEVICE_ID}.{LEGACY_SECRET}"


class FakeManagedProvider(TunnelProvider):
    def __init__(self) -> None:
        self.urls = {8080: MANAGED_ORIGIN, 8081: CHANGED_ORIGIN}
        self.active: dict[int, str] = {}

    def start(self, port: int, label: str = "") -> str:  # noqa: ARG002
        self.active[port] = self.urls[port]
        return self.active[port]

    def stop(self, port: int) -> None:
        self.active.pop(port, None)

    def stop_all(self) -> None:
        self.active.clear()

    def get_url(self, port: int) -> str | None:
        return self.active.get(port)

    def is_available(self) -> bool:
        return True

    def active_tunnels(self) -> dict[int, str]:
        return dict(self.active)


class CountingAuthenticator:
    def __init__(self, authenticator) -> None:
        self.authenticator = authenticator
        self.calls = 0

    def authenticate_scope(self, scope, provenance):
        self.calls += 1
        return self.authenticator.authenticate_scope(scope, provenance)


def _legacy_database(path) -> None:
    verifier = hash_secret(LEGACY_SECRET)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE mobile_devices(
              id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              token_hash TEXT NOT NULL UNIQUE,
              token_salt TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_seen_at TEXT,
              revoked_at TEXT,
              user_agent TEXT,
              paired_from TEXT,
              access_mode TEXT,
              scopes_json TEXT NOT NULL
            );
            CREATE TABLE mobile_pairing_codes(
              id TEXT PRIMARY KEY,
              code_hash TEXT NOT NULL UNIQUE,
              code_salt TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              claimed_at TEXT,
              intended_origin TEXT,
              access_mode TEXT,
              failed_attempts INTEGER NOT NULL DEFAULT 0,
              locked_until TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO mobile_devices
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                LEGACY_DEVICE_ID,
                "Existing phone",
                verifier.secret_hash,
                verifier.salt,
                now,
                None,
                None,
                "legacy-agent",
                "198.51.100.25",
                "ngrok",
                '["settings"]',
            ),
        )


def _proxy_headers(origin: str, *, cookie: str | None = None) -> dict[str, str]:
    host = origin.removeprefix("https://")
    headers = {
        "host": host,
        "x-forwarded-for": "198.51.100.25",
        "x-forwarded-host": host,
        "x-forwarded-proto": "https",
    }
    if cookie is not None:
        headers["cookie"] = f"{LEGACY_HTTPS_COOKIE_NAME}={cookie}"
    return headers


def _build_app(path):
    _legacy_database(path)
    store = AccessStore(path)
    store.ensure_schema()
    service = AccessService(store)
    app = FastAPI()

    @app.get("/")
    async def root(request: Request):
        context = request.scope[ACCESS_CONTEXT_SCOPE_KEY]
        return {
            "authentication_kind": context.authentication_kind.value,
            "device_id": context.device_id,
            "origin": context.origin,
        }

    @app.websocket("/_nicegui_ws/socket.io")
    async def websocket_route(websocket: WebSocket):
        await websocket.accept()
        message = await websocket.receive_text()
        await websocket.send_text(message)
        await websocket.close()

    registration = register_access_routes(
        app,
        service=service,
        config=AccessConfig.build(
            deployment_mode="server",
            allowed_hosts=("localhost",),
        ),
    )
    runtime_policy = RuntimeAccessPolicy(registration.config)
    authenticator = CountingAuthenticator(registration.authenticator)
    wrapped = AccessMiddleware(
        app,
        runtime_policy=runtime_policy,
        session_authenticator=authenticator,
        websocket_revalidation_seconds=0.05,
    )
    manager = TunnelManager(managed_origin_registrar=runtime_policy)
    manager.set_provider(FakeManagedProvider())
    client = TestClient(
        wrapped,
        base_url=MANAGED_ORIGIN,
        client=("127.0.0.1", 51000),
        follow_redirects=False,
    )
    return client, manager, runtime_policy, service, authenticator


def test_migrated_mobile_cookie_survives_same_managed_ngrok_host(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    client, manager, runtime_policy, service, authenticator = _build_app(
        tmp_path / "mobile.db"
    )

    assert manager.start_tunnel(8080, label="main_app") == MANAGED_ORIGIN
    admitted = client.get(
        "/",
        headers=_proxy_headers(MANAGED_ORIGIN, cookie=LEGACY_TOKEN),
    )

    assert admitted.status_code == 200
    assert admitted.json() == {
        "authentication_kind": "session",
        "device_id": LEGACY_DEVICE_ID,
        "origin": MANAGED_ORIGIN,
    }
    assert service.list_devices()[0].legacy_source_id == LEGACY_DEVICE_ID
    assert service.list_invitations() == []

    calls_before_unknown = authenticator.calls
    unknown = client.get(
        "/",
        headers=_proxy_headers(
            "https://unknown-phone.ngrok-free.app",
            cookie=LEGACY_TOKEN,
        ),
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "untrusted_forwarding_headers"
    assert authenticator.calls == calls_before_unknown

    assert manager.start_tunnel(8081, label="rotated") == CHANGED_ORIGIN
    changed_without_cookie = client.get(
        "/",
        headers={
            **_proxy_headers(CHANGED_ORIGIN),
            "accept": "text/html",
        },
    )
    assert changed_without_cookie.status_code == 303
    assert changed_without_cookie.headers["location"].startswith("/connect?next=")
    assert service.list_invitations() == []

    with client.websocket_connect(
        "/_nicegui_ws/socket.io",
        headers={
            **_proxy_headers(MANAGED_ORIGIN, cookie=LEGACY_TOKEN),
            "origin": MANAGED_ORIGIN,
        },
    ) as websocket:
        websocket.send_text("paired")
        assert websocket.receive_text() == "paired"

    manager.stop_tunnel(8080)
    assert MANAGED_ORIGIN not in runtime_policy.snapshot().managed_origins
    stopped = client.get(
        "/",
        headers={"host": "stable-phone.ngrok-free.app"},
    )
    assert stopped.status_code == 400
    assert stopped.json()["error"] == "unexpected_host"

    with pytest.raises(WebSocketDisconnect) as stopped_websocket:
        with client.websocket_connect(
            "/_nicegui_ws/socket.io",
            headers={
                "host": "stable-phone.ngrok-free.app",
                "origin": MANAGED_ORIGIN,
                "cookie": f"{LEGACY_HTTPS_COOKIE_NAME}={LEGACY_TOKEN}",
            },
        ):
            pass
    assert stopped_websocket.value.code == 1008
