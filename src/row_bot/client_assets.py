"""Opt-in local client assets, behind the host's existing access middleware."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import stat
import sys

from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse, Response

from row_bot.access.policy import require_authenticated_owner

_HASHED_ASSET = re.compile(r"assets/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8,}\.(?:js|css|svg|png|jpg|jpeg|webp|ico|woff2?)$")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_MIME = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".ico": "image/x-icon", ".woff": "font/woff",
         ".woff2": "font/woff2", ".html": "text/html"}
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_FILE_BYTES = 8 * 1024 * 1024


class AssetValidationError(ValueError):
    """The local build is absent, incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class ClientAsset:
    """Verified immutable build bytes; no filesystem lookup during delivery."""

    content: bytes
    media_type: str
    sha256: str


class _ShellScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.inline: list[str] = []
        self._script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and not dict(attrs).get("src"):
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.inline.append("".join(self._script))
            self._script = None


def _safe_parts(name: str) -> tuple[str, ...]:
    if not name or any(character in name for character in ("\\", "%", ":", "\x00")):
        raise AssetValidationError("invalid_asset_name")
    parts = tuple(name.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise AssetValidationError("invalid_asset_name")
    return parts


def _read_regular(root: Path, name: str, maximum: int) -> bytes:
    path = root
    # Reject symlinks and Windows junction/reparse points at every boundary.
    for part in _safe_parts(name):
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AssetValidationError("linked_asset")
    if not path.resolve().is_relative_to(root.resolve()) or not stat.S_ISREG(info.st_mode):
        raise AssetValidationError("invalid_asset_file")
    if info.st_size > maximum:
        raise AssetValidationError("asset_too_large")
    # A bounded read followed by digest verification prevents a swapped file
    # from contributing unverified bytes. Serving retains these exact bytes.
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise AssetValidationError("asset_too_large")
    return content


def load_client_assets(root: Path) -> dict[str, ClientAsset]:
    """Validate a complete generated inventory and materialize bounded assets."""
    try:
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AssetValidationError("invalid_asset_root")
        manifest = json.loads(_read_regular(root, "asset-manifest.json", 256 * 1024))
        if (not isinstance(manifest, dict) or set(manifest) != {"version", "files"}
                or type(manifest["version"]) is not int or manifest["version"] != 1):
            raise AssetValidationError("invalid_asset_manifest")
        entries = manifest["files"]
        if not isinstance(entries, dict) or "index.html" not in entries or not 2 <= len(entries) <= 512:
            raise AssetValidationError("invalid_asset_manifest")
        result: dict[str, ClientAsset] = {}
        total = 0
        for name, entry in entries.items():
            if name != "index.html" and not _HASHED_ASSET.fullmatch(name):
                raise AssetValidationError("unhashed_asset")
            if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                    or not isinstance(entry["sha256"], str) or not _DIGEST.fullmatch(entry["sha256"])
                    or type(entry["size"]) is not int or not 0 <= entry["size"] <= _MAX_FILE_BYTES):
                raise AssetValidationError("invalid_asset_manifest")
            total += entry["size"]
            if total > _MAX_TOTAL_BYTES:
                raise AssetValidationError("build_too_large")
            content = _read_regular(root, name, 256 * 1024 if name == "index.html" else _MAX_FILE_BYTES)
            digest = hashlib.sha256(content).hexdigest()
            if len(content) != entry["size"] or digest != entry["sha256"]:
                raise AssetValidationError("asset_integrity_mismatch")
            result[name] = ClientAsset(content, _MIME[Path(name).suffix], digest)
        vite = json.loads(_read_regular(root, ".vite/manifest.json", 256 * 1024))
        if (not isinstance(vite, dict) or not isinstance(vite.get("index.html"), dict)
                or vite["index.html"].get("isEntry") is not True):
            raise AssetValidationError("invalid_vite_manifest")
        for entry in vite.values():
            if not isinstance(entry, dict) or entry.get("file") not in result:
                raise AssetValidationError("invalid_vite_manifest")
            for key in ("css", "assets", "imports", "dynamicImports"):
                values = entry.get(key, [])
                if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                    raise AssetValidationError("invalid_vite_manifest")
            for name in entry.get("css", []) + entry.get("assets", []):
                if name not in result:
                    raise AssetValidationError("missing_build_asset")
            for key in entry.get("imports", []) + entry.get("dynamicImports", []):
                if key not in vite:
                    raise AssetValidationError("missing_build_chunk")
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AssetValidationError("client_build_unavailable") from exc


def default_client_asset_root() -> Path:
    """Use checkout output during development, bundled assets when installed."""
    package = Path(__file__).resolve().parent
    checkout = package.parent.parent
    if not getattr(sys, "frozen", False) and (checkout / "frontend/package.json").is_file():
        return checkout / "frontend/dist"
    return package / "static/client-v2"


def _shell_headers(content: bytes) -> dict[str, str]:
    parser = _ShellScripts()
    parser.feed(content.decode("utf-8"))
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"
                      for script in parser.inline)
    return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' " + hashes + "; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
            "font-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"}


def install_client_assets(app: FastAPI, *, asset_root: Path | None = None) -> None:
    """Mount only /app-v2, preserving the host's root, lifespan and access owner.

    Invalid/missing builds give a safe 503 only on the opt-in client. This helper
    also requires access context itself so accidentally mounting without the
    shared middleware cannot expose the shell or assets.
    """
    if getattr(app.state, "row_bot_client_assets_installed", False):
        return
    app.state.row_bot_client_assets_installed = True
    try:
        assets = load_client_assets(asset_root or default_client_asset_root())
        shell_headers = _shell_headers(assets["index.html"].content)
    except (AssetValidationError, UnicodeError):
        assets = {}
        shell_headers = {}

    async def serve(request: Request, path: str = "") -> Response:
        try:
            require_authenticated_owner(request.scope)
        except PermissionError:
            return Response("Authentication required", 401, headers={"Cache-Control": "no-store"})
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        try:
            if path:
                _safe_parts(path.rstrip("/"))
        except AssetValidationError:
            return Response("Not found", 404, headers=headers)
        if not assets:
            return Response("Client preview is not built. Build the local frontend and restart the host.",
                            503, media_type="text/plain", headers=headers)
        asset = assets.get(path)
        if asset is not None and path != "index.html":
            return Response(asset.content if request.method != "HEAD" else b"", media_type=asset.media_type,
                            headers={"Cache-Control": "private, max-age=31536000, immutable",
                                     "ETag": '"' + asset.sha256 + '"',
                                     "Content-Length": str(len(asset.content)),
                                     "X-Content-Type-Options": "nosniff"})
        if path not in {"", "index.html"} and ("." in path or path.startswith("assets/")
                                                or "text/html" not in request.headers.get("accept", "")):
            return Response("Not found", 404, headers=headers)
        return Response(assets["index.html"].content if request.method != "HEAD" else b"",
                        media_type="text/html", headers={**shell_headers, "Content-Length": str(len(assets["index.html"].content))})

    async def redirect(request: Request) -> Response:
        try:
            require_authenticated_owner(request.scope)
        except PermissionError:
            return Response("Authentication required", 401, headers={"Cache-Control": "no-store"})
        return RedirectResponse("/app-v2/", status_code=307, headers={"Cache-Control": "no-store"})

    app.add_api_route("/app-v2", redirect, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/app-v2/{path:path}", serve, methods=["GET", "HEAD"], include_in_schema=False)
