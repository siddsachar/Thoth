from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
import pytest
from starlette.testclient import TestClient

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.request_context import SessionIdentity
from row_bot.client_assets import AssetValidationError, install_client_assets, load_client_assets


@pytest.fixture
def build(tmp_path: Path) -> Path:
    root = tmp_path / "client"
    (root / "assets").mkdir(parents=True)
    (root / ".vite").mkdir()
    files = {"index.html": b'<html><head><script>window.theme="dark";</script></head><body><script type="module" src="/app-v2/assets/index-abcdef12.js"></script></body></html>',
             "assets/index-abcdef12.js": b"export const fixture = true;",
             "assets/index-abcdef12.css": b"body { color: black }"}
    for name, data in files.items():
        (root / name).write_bytes(data)
    (root / "asset-manifest.json").write_text(json.dumps({"version": 1, "files": {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in files.items()}}))
    (root / ".vite/manifest.json").write_text(json.dumps({"index.html": {
        "file": "assets/index-abcdef12.js", "isEntry": True, "css": ["assets/index-abcdef12.css"]}}))
    return root


def client(root: Path, *, mode: str = "desktop", middleware: bool = True) -> TestClient:
    app = FastAPI()
    @app.get("/")
    def root_route():
        return {"surface": "NiceGUI fixture"}
    @app.get("/connect")
    def connect():
        return {"surface": "connect"}
    @app.get("/mobile/pair")
    def pair():
        return {"surface": "pair"}
    install_client_assets(app, asset_root=root)
    install_client_assets(app, asset_root=root)
    host = AccessMiddleware(app, config=AccessConfig.build(deployment_mode=mode, allowed_hosts=("localhost",)),
                            session_authenticator=lambda scope, _provenance: SessionIdentity(device_id="fixture", session_id="fixture")
                            if (b"x-fixture-session", b"yes") in scope["headers"] else None) if middleware else app
    return TestClient(host, base_url="http://localhost:8080", client=("127.0.0.1", 51000), follow_redirects=False)


def test_dual_host_cache_history_and_private_manifest(build: Path) -> None:
    host = client(build)
    assert host.get("/").json() == {"surface": "NiceGUI fixture"}
    assert host.get("/app-v2").headers["location"] == "/app-v2/"
    shell = host.get("/app-v2/")
    assert shell.status_code == 200 and shell.headers["cache-control"] == "no-store"
    assert "sha256-" in shell.headers["content-security-policy"]
    assert "frame-src 'none'" in shell.headers["content-security-policy"]
    assert host.get("/app-v2/conversations/fixture", headers={"Accept": "text/html"}).content == shell.content
    assert host.head("/app-v2/").content == b""
    assert host.head("/app-v2/").headers["content-length"] == str(len(shell.content))
    asset = host.get("/app-v2/assets/index-abcdef12.js")
    assert "immutable" in asset.headers["cache-control"]
    assert asset.headers["x-content-type-options"] == "nosniff"
    for path in (".vite/manifest.json", "asset-manifest.json", "assets/missing.js", "assets/missing", "secret.txt", "%252e%252e/secret", "a%5cb"):
        assert host.get("/app-v2/" + path, headers={"Accept": "text/html"}).status_code == 404
    assert host.get("/app-v2/conversation").status_code == 404
    assert host.post("/app-v2/", headers={"Origin": "http://localhost:8080"}).status_code == 405


def test_assets_require_same_host_auth_and_pairing_remains_public(build: Path) -> None:
    host = client(build, mode="server")
    assert host.get("/app-v2/", headers={"Accept": "text/html"}).headers["location"].startswith("/connect?")
    assert host.get("/app-v2/assets/index-abcdef12.js").status_code == 401
    assert host.get("/app-v2/", headers={"x-fixture-session": "yes"}).status_code == 200
    assert host.get("/app-v2/", headers={"Host": "foreign.invalid"}).status_code == 400
    assert host.get("/connect").status_code == host.get("/mobile/pair").status_code == 200
    assert client(build, middleware=False).get("/app-v2/").status_code == 401


def test_missing_build_does_not_break_existing_root(tmp_path: Path) -> None:
    host = client(tmp_path / "absent")
    assert host.get("/").status_code == 200
    assert host.get("/app-v2/").status_code == 503


def test_preloaded_bytes_remain_exact_after_disk_swap(build: Path) -> None:
    host = client(build)
    expected = host.get("/app-v2/assets/index-abcdef12.js").content
    (build / "assets/index-abcdef12.js").write_text("private replacement")
    assert host.get("/app-v2/assets/index-abcdef12.js").content == expected
    with pytest.raises(AssetValidationError):
        load_client_assets(build)


@pytest.mark.parametrize("change", ["path", "hash", "size", "version", "unhashed", "missing_chunk", "missing_asset"])
def test_rejects_manifest_and_vite_corruption(build: Path, change: str) -> None:
    path = build / "asset-manifest.json"
    manifest = json.loads(path.read_text())
    if change == "path":
        manifest["files"]["../private"] = manifest["files"].pop("assets/index-abcdef12.js")
    elif change == "hash":
        manifest["files"]["index.html"]["sha256"] = "a" * 64
    elif change == "size":
        manifest["files"]["index.html"]["size"] = 90 * 1024 * 1024
    elif change == "version":
        manifest["version"] = 2
    elif change == "unhashed":
        manifest["files"]["assets/plain.js"] = manifest["files"].pop("assets/index-abcdef12.js")
    else:
        vite = json.loads((build / ".vite/manifest.json").read_text())
        vite["index.html"]["imports" if change == "missing_chunk" else "css"] = ["missing"]
        (build / ".vite/manifest.json").write_text(json.dumps(vite))
    path.write_text(json.dumps(manifest))
    with pytest.raises(AssetValidationError):
        load_client_assets(build)


def test_reparse_boundary_is_rejected_without_os_symlink_permission(build: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.lstat
    class LinkedStat:
        st_mode = 0o120777
        st_file_attributes = 0x400
    monkeypatch.setattr(Path, "lstat", lambda path, **kwargs: LinkedStat() if path == build / "assets" else original(path, **kwargs))
    with pytest.raises(AssetValidationError):
        load_client_assets(build)
