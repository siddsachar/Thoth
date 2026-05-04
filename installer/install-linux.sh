#!/usr/bin/env bash
# Install the latest Thoth Linux XDG tarball from GitHub Releases.

set -euo pipefail

REPO="${THOTH_REPO:-siddsachar/Thoth}"
API_BASE="https://api.github.com/repos/${REPO}"
REQUESTED_VERSION="${1:-${THOTH_VERSION:-latest}}"
USER_AGENT="Thoth-Linux-Installer"

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[ OK ] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

need_cmd curl
need_cmd python3
need_cmd sha256sum
need_cmd tar

case "$(uname -m)" in
    x86_64|amd64) PACKAGE_ARCH="x86_64" ;;
    aarch64|arm64) PACKAGE_ARCH="aarch64" ;;
    *) fail "Unsupported Linux architecture: $(uname -m)" ;;
esac

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/thoth-linux-install.XXXXXX")"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

if [ "$REQUESTED_VERSION" = "latest" ]; then
    RELEASE_URL="${API_BASE}/releases/latest"
else
    RELEASE_URL="${API_BASE}/releases/tags/v${REQUESTED_VERSION#v}"
fi

RELEASE_JSON="$WORK_DIR/release.json"
curl_args=(
    -fsSL
    --retry 3
    --connect-timeout 15
    -H "Accept: application/vnd.github+json"
    -H "User-Agent: ${USER_AGENT}"
)
if [ -n "${GITHUB_TOKEN:-${THOTH_INSTALL_TOKEN:-}}" ]; then
    curl_args+=( -H "Authorization: Bearer ${GITHUB_TOKEN:-${THOTH_INSTALL_TOKEN:-}}" )
fi

info "Resolving Thoth ${REQUESTED_VERSION} Linux package for ${PACKAGE_ARCH}..."
curl "${curl_args[@]}" "$RELEASE_URL" -o "$RELEASE_JSON"

META_FILE="$WORK_DIR/release-meta.txt"
python3 - "$RELEASE_JSON" "$PACKAGE_ARCH" > "$META_FILE" <<'PY'
import json
import re
import sys

release_path, arch = sys.argv[1:3]
with open(release_path, "r", encoding="utf-8") as handle:
    release = json.load(handle)

tag = str(release.get("tag_name") or "").lstrip("v")
if not tag:
    raise SystemExit("Release has no tag_name")

asset_pattern = re.compile(rf"^Thoth-{re.escape(tag)}-Linux-{re.escape(arch)}\.tar\.gz$")
asset = None
for candidate in release.get("assets") or []:
    name = str(candidate.get("name") or "")
    if asset_pattern.match(name):
        asset = candidate
        break
if asset is None:
    fallback = re.compile(rf"^Thoth-[^-]+-Linux-{re.escape(arch)}\.tar\.gz$")
    for candidate in release.get("assets") or []:
        name = str(candidate.get("name") or "")
        if fallback.match(name):
            asset = candidate
            break
if asset is None:
    raise SystemExit(f"No Linux {arch} tarball asset found on release v{tag}")

asset_name = str(asset.get("name") or "")
asset_url = str(asset.get("browser_download_url") or "")
if not asset_url.startswith("https://"):
    raise SystemExit(f"Invalid asset URL for {asset_name}")

body = str(release.get("body") or "")
block = re.search(
    r"<!--\s*thoth-update-manifest\s*-->\s*```manifest\s*(.*?)\s*```",
    body,
    re.DOTALL | re.IGNORECASE,
)
sha256 = ""
if block:
    for line in block.group(1).splitlines():
        match = re.match(r"^\s*([A-Za-z0-9._+-]+):\s*sha256\s*=\s*([0-9a-fA-F]{64})\s*$", line)
        if match and match.group(1) == asset_name:
            sha256 = match.group(2).lower()
            break
if not sha256:
    raise SystemExit(f"Release manifest is missing SHA256 for {asset_name}")

print(asset_name)
print(asset_url)
print(sha256)
print(tag)
PY
mapfile -t release_meta < "$META_FILE"
if [ "${#release_meta[@]}" -ne 4 ]; then
    fail "Could not parse release metadata"
fi

ASSET_NAME="${release_meta[0]}"
ASSET_URL="${release_meta[1]}"
SHA256="${release_meta[2]}"
VERSION="${release_meta[3]}"
TARBALL="$WORK_DIR/$ASSET_NAME"

info "Downloading ${ASSET_NAME}..."
curl -fL --retry 3 --connect-timeout 15 -H "User-Agent: ${USER_AGENT}" "$ASSET_URL" -o "$TARBALL"

info "Verifying SHA256..."
printf '%s  %s\n' "$SHA256" "$TARBALL" | sha256sum -c - >/dev/null
ok "SHA256 verified"

EXTRACT_DIR="$WORK_DIR/extract"
mkdir -p "$EXTRACT_DIR"
tar -xzf "$TARBALL" -C "$EXTRACT_DIR"
PACKAGE_ROOT="$(find "$EXTRACT_DIR" -maxdepth 1 -type d -name "Thoth-*-Linux-${PACKAGE_ARCH}" | head -n 1)"
if [ -z "$PACKAGE_ROOT" ] || [ ! -f "$PACKAGE_ROOT/install.sh" ]; then
    fail "Downloaded package is missing install.sh"
fi

info "Installing Thoth ${VERSION}..."
bash "$PACKAGE_ROOT/install.sh"

case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *) warn "${HOME}/.local/bin is not on PATH. Add it to your shell profile or run ${HOME}/.local/bin/thoth." ;;
esac

ok "Thoth ${VERSION} installed. Run: thoth"