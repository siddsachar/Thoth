from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from row_bot.mobile.access_gate import MobileAccessGate
from row_bot.mobile.routes import register_mobile_routes
from row_bot.mobile.store import MobileAuthStore


def _client(tmp_path) -> TestClient:
    app = FastAPI()
    store = MobileAuthStore(tmp_path / "mobile.db")
    register_mobile_routes(app, store=store)
    app.add_middleware(MobileAccessGate, store=store)
    return TestClient(
        app,
        base_url="http://localhost:8080",
        client=("192.168.1.25", 50000),
    )


def test_manifest_is_valid_and_publicly_pairing_gate_accessible(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/mobile/manifest.webmanifest")

    assert response.status_code == 200
    manifest = response.json()
    assert manifest["start_url"] == "/?mobile=1"
    assert manifest["display"] == "standalone"
    assert manifest["icons"][0]["src"] == "/static/row_bot_glyph_256.png"


def test_service_worker_does_not_cache_private_surfaces(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/mobile/service-worker.js")

    assert response.status_code == 200
    body = response.text
    assert "row-bot-mobile-shell-v2" in body
    assert "/mobile/offline" in body
    assert "/static/row_bot_glyph_256.png" in body
    assert "PRIVATE_PREFIXES" in body
    assert "BYPASS_PATHS" in body
    assert "'/mobile/pair'" in body
    assert "'/api/'" in body
    assert "'/_media'" in body
    assert "'/published'" in body
    assert "cache.put" not in body


def test_offline_page_is_available_without_pairing(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/mobile/offline")

    assert response.status_code == 200
    assert "not reachable" in response.text
