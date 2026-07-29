from __future__ import annotations

from fastapi import FastAPI, Request, WebSocket
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.request_context import (
    ACCESS_CONTEXT_SCOPE_KEY,
    SessionIdentity,
)
from row_bot.access.runtime_policy import RuntimeAccessPolicy
from row_bot.access.tailscale import (
    OWNERSHIP_SCHEMA_VERSION,
    TailscaleOwnership,
    augment_access_config_for_owned_tailscale,
)


def _session_authenticator(scope, provenance):  # noqa: ARG001
    headers = {
        bytes(name).lower(): bytes(value).decode("latin-1")
        for name, value in scope.get("headers", [])
    }
    profile = headers.get(b"x-test-profile")
    if profile not in {"owner", "companion"}:
        return None
    return SessionIdentity(
        profile=profile,
        device_id=f"{profile}-device",
        session_id=f"{profile}-session",
    )


def _app(
    config: AccessConfig,
    *,
    runtime_policy: RuntimeAccessPolicy | None = None,
    session_authenticator=_session_authenticator,
) -> TestClient:
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"surface": "root"}

    @app.get("/connect")
    async def connect():
        return {"surface": "connect"}

    @app.get("/api/private")
    async def private():
        return {"private": True}

    @app.get("/api/access/devices")
    async def devices():
        return {"devices": []}

    @app.post("/api/access/devices/{device_id}/revoke")
    async def revoke(device_id: str):
        return {"revoked": device_id}

    @app.post("/api/launcher-shutdown")
    async def launcher_shutdown(request: Request):
        if request.headers.get("x-test-launch-secret") != "expected-secret":
            return JSONResponse(
                {"ok": False, "error": "bad_launch_secret"},
                status_code=403,
            )
        return {"shutdown": True}

    @app.post("/api/webhook/{task_id}")
    async def webhook(task_id: str):
        return {"task": task_id}

    @app.get("/healthz")
    async def health():
        return JSONResponse({"ok": True})

    @app.get("/readyz")
    async def ready():
        return JSONResponse({"ready": True})

    @app.get("/context")
    async def context(request: Request):
        context = request.scope[ACCESS_CONTEXT_SCOPE_KEY]
        return {
            "profile": context.profile,
            "origin": context.origin,
            "effective_client": context.effective_client,
        }

    @app.websocket("/ws")
    async def websocket_route(websocket: WebSocket):
        await websocket.accept()
        value = await websocket.receive_text()
        await websocket.send_text(value)
        await websocket.close()

    middleware_options = {
        "session_authenticator": session_authenticator,
        "websocket_revalidation_seconds": 0.05,
    }
    if runtime_policy is None:
        middleware_options["config"] = config
    else:
        middleware_options["runtime_policy"] = runtime_policy
    wrapped = AccessMiddleware(app, **middleware_options)
    return TestClient(
        wrapped,
        base_url="http://localhost:8080",
        client=("127.0.0.1", 51000),
        follow_redirects=False,
    )


def _server_config(**kwargs) -> AccessConfig:
    values = {
        "deployment_mode": "server",
        "allowed_hosts": ("localhost",),
    }
    values.update(kwargs)
    return AccessConfig.build(**values)


def test_desktop_loopback_passes_and_context_reaches_downstream() -> None:
    client = _app(
        AccessConfig.build(deployment_mode="desktop", allowed_hosts=("localhost",))
    )

    response = client.get("/")
    context = client.get("/context")

    assert response.status_code == 200
    assert context.json()["profile"] == "owner"


def test_server_loopback_and_remote_browser_redirect_to_neutral_connect() -> None:
    client = _app(_server_config())

    response = client.get("/", headers={"accept": "text/html"})

    assert response.status_code == 303
    assert response.headers["location"] == "/connect?next=%2F"
    assert response.headers["cache-control"] == "no-store"


def test_unpaired_api_is_json_401_and_public_probes_are_minimal() -> None:
    client = _app(_server_config())

    private = client.get("/api/private")
    health = client.get("/healthz")
    ready = client.get("/readyz")

    assert private.status_code == 401
    assert private.headers["content-type"].startswith("application/json")
    assert private.json() == {"ok": False, "error": "authentication_required"}
    assert health.json() == {"ok": True}
    assert ready.json() == {"ready": True}
    assert "version" not in health.text
    assert "path" not in ready.text


def test_owner_session_can_use_full_api_but_companion_cannot_administer_access() -> (
    None
):
    client = _app(_server_config())

    owner = client.get("/api/access/devices", headers={"x-test-profile": "owner"})
    companion = client.get(
        "/api/access/devices", headers={"x-test-profile": "companion"}
    )
    companion_private = client.get(
        "/api/private", headers={"x-test-profile": "companion"}
    )

    assert owner.status_code == 200
    assert companion.status_code == 403
    assert companion.json()["error"] == "capability_required"
    assert companion_private.status_code == 200


def test_state_changing_session_route_requires_exact_same_origin() -> None:
    client = _app(_server_config())

    missing = client.post(
        "/api/access/devices/device/revoke",
        headers={"x-test-profile": "owner"},
    )
    wrong = client.post(
        "/api/access/devices/device/revoke",
        headers={
            "x-test-profile": "owner",
            "origin": "https://attacker.example",
        },
    )
    allowed = client.post(
        "/api/access/devices/device/revoke",
        headers={
            "x-test-profile": "owner",
            "origin": "http://localhost:8080",
        },
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert allowed.status_code == 200


def test_unexpected_host_and_untrusted_forwarding_are_rejected() -> None:
    client = _app(_server_config())

    host = client.get("/connect", headers={"host": "attacker.example"})
    forwarded = client.get("/connect", headers={"x-forwarded-for": "127.0.0.1"})

    assert host.status_code == 400
    assert host.json()["error"] == "unexpected_host"
    assert forwarded.status_code == 400
    assert forwarded.json()["error"] == "untrusted_forwarding_headers"


def test_trusted_proxy_supplies_validated_origin_and_effective_client() -> None:
    client = _app(
        _server_config(
            trusted_proxy_cidrs=("127.0.0.0/8",),
            allowed_hosts=("rowbot.example.com",),
            public_origins=("https://rowbot.example.com",),
        )
    )

    response = client.get(
        "http://rowbot.example.com/context",
        headers={
            "host": "rowbot.example.com",
            "forwarded": ("for=192.0.2.8;proto=https;host=rowbot.example.com"),
            "x-test-profile": "owner",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "profile": "owner",
        "origin": "https://rowbot.example.com",
        "effective_client": "192.0.2.8",
    }


def test_owned_tailscale_route_accepts_its_loopback_forwarding_headers() -> None:
    origin = "https://rowbot-device.example.ts.net"
    ownership = TailscaleOwnership(
        schema_version=OWNERSHIP_SCHEMA_VERSION,
        config_fingerprint="0" * 64,
        origin=origin,
        target="http://127.0.0.1:8080",
        path="/",
        https_port=443,
    )
    config = augment_access_config_for_owned_tailscale(
        AccessConfig.build(),
        ownership=ownership,
        app_port=8080,
    )
    client = _app(config)

    response = client.get(
        f"{origin}/context",
        headers={
            "host": "rowbot-device.example.ts.net",
            "x-forwarded-for": "100.64.0.8",
            "x-forwarded-host": "rowbot-device.example.ts.net",
            "x-forwarded-proto": "https",
            "x-test-profile": "owner",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "profile": "owner",
        "origin": origin,
        "effective_client": "100.64.0.8",
    }


def test_http_uses_one_runtime_policy_snapshot_during_authentication() -> None:
    origin = "https://managed-example.ngrok-free.app"
    policy = RuntimeAccessPolicy(_server_config())
    policy.register_managed_origin(origin)
    authentication_calls = 0

    def unregistering_authenticator(scope, provenance):  # noqa: ARG001
        nonlocal authentication_calls
        authentication_calls += 1
        policy.unregister_managed_origin(origin)
        return SessionIdentity(
            profile="owner",
            device_id="owner-device",
            session_id="owner-session",
        )

    client = _app(
        policy.base_config,
        runtime_policy=policy,
        session_authenticator=unregistering_authenticator,
    )
    headers = {
        "host": "managed-example.ngrok-free.app",
        "x-forwarded-for": "198.51.100.25",
        "x-forwarded-host": "managed-example.ngrok-free.app",
        "x-forwarded-proto": "https",
    }

    admitted = client.get("/context", headers=headers)
    rejected = client.get(
        "/context",
        headers={"host": "managed-example.ngrok-free.app"},
    )

    assert admitted.status_code == 200
    assert admitted.json()["origin"] == origin
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "unexpected_host"
    assert authentication_calls == 1


def test_websocket_uses_one_runtime_policy_snapshot_for_admission() -> None:
    origin = "https://managed-example.ngrok-free.app"
    policy = RuntimeAccessPolicy(_server_config())
    policy.register_managed_origin(origin)
    removed = False

    def unregistering_authenticator(scope, provenance):  # noqa: ARG001
        nonlocal removed
        if not removed:
            removed = True
            policy.unregister_managed_origin(origin)
        return SessionIdentity(
            profile="owner",
            device_id="owner-device",
            session_id="owner-session",
        )

    client = _app(
        policy.base_config,
        runtime_policy=policy,
        session_authenticator=unregistering_authenticator,
    )
    headers = {
        "host": "managed-example.ngrok-free.app",
        "origin": origin,
        "x-forwarded-for": "198.51.100.25",
        "x-forwarded-host": "managed-example.ngrok-free.app",
        "x-forwarded-proto": "https",
    }

    with client.websocket_connect("/ws", headers=headers) as websocket:
        websocket.send_text("coherent")
        assert websocket.receive_text() == "coherent"

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            "/ws",
            headers={
                "host": "managed-example.ngrok-free.app",
                "origin": origin,
            },
        ):
            pass
    assert rejected.value.code == 1008


@pytest.mark.parametrize("profile", ["owner", "companion"])
def test_launcher_operation_rejects_authenticated_remote_session(
    profile: str,
) -> None:
    remote = TestClient(
        AccessMiddleware(
            FastAPI(),
            config=_server_config(),
            session_authenticator=_session_authenticator,
        ),
        base_url="http://localhost:8080",
        client=("192.168.1.10", 51000),
    )

    response = remote.post(
        "/api/launcher-shutdown",
        headers={
            "x-test-profile": profile,
            "origin": "http://localhost:8080",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"] == "launcher_local_only"


def test_launcher_handler_secret_is_separate_from_transport_gate() -> None:
    client = _app(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )

    missing = client.post("/api/launcher-shutdown")
    wrong = client.post(
        "/api/launcher-shutdown", headers={"x-test-launch-secret": "wrong"}
    )
    allowed = client.post(
        "/api/launcher-shutdown",
        headers={"x-test-launch-secret": "expected-secret"},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert allowed.status_code == 200


def test_forwarded_loopback_does_not_reach_launcher_handler_boundary() -> None:
    client = _app(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )

    response = client.post(
        "/api/launcher-shutdown",
        headers={
            "x-forwarded-for": "127.0.0.1",
            "x-test-launch-secret": "expected-secret",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "untrusted_forwarding_headers"


def test_websocket_requires_session_and_exact_origin() -> None:
    client = _app(_server_config())

    with pytest.raises(WebSocketDisconnect) as unpaired:
        with client.websocket_connect(
            "/ws",
            headers={
                "host": "localhost:8080",
                "origin": "http://localhost:8080",
            },
        ):
            pass
    with pytest.raises(WebSocketDisconnect) as wrong_origin:
        with client.websocket_connect(
            "/ws",
            headers={
                "origin": "https://attacker.example",
                "host": "localhost:8080",
                "x-test-profile": "owner",
            },
        ):
            pass

    assert unpaired.value.code == 1008
    assert wrong_origin.value.code == 1008
    with client.websocket_connect(
        "/ws",
        headers={
            "origin": "http://localhost:8080",
            "host": "localhost:8080",
            "x-test-profile": "owner",
        },
    ) as websocket:
        websocket.send_text("ok")
        assert websocket.receive_text() == "ok"


def test_webhook_authentication_remains_owned_by_webhook_route() -> None:
    client = _app(_server_config())

    response = client.post("/api/webhook/task-1", json={"safe": True})

    assert response.status_code == 200
    assert response.json() == {"task": "task-1"}
