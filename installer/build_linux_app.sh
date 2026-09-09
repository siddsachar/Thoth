#!/usr/bin/env bash
# =============================================================================
# build_linux_app.sh - Build self-contained Row-Bot Linux tarball
#
# Produces dist/Row-Bot-X.Y.Z-Linux-ARCH.tar.gz using python-build-standalone.
# The artifact installs into XDG user paths via install.sh and launches in
# browser/no-tray mode by default so Linux desktop system dependencies stay
# optional.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_VERSION="$(python3 - "$PROJECT_DIR/src/row_bot/version.py" <<'PY'
import sys
from pathlib import Path
ns = {}
exec(Path(sys.argv[1]).read_text(encoding='utf-8'), ns)
print(ns.get('__version__', '0.0.0'))
PY
)"
VERSION="${1:-$DEFAULT_VERSION}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12.13}"
PBS_RELEASE="${PBS_RELEASE:-20260303}"
BUILD_DIR="$SCRIPT_DIR/build/linux"
DIST_DIR="$PROJECT_DIR/dist"

# Linux browser bundling policy. Default is 0 because system dependencies for
# Chromium are distro-specific; CI and local release builds can force 1.
BUNDLE_PLAYWRIGHT="${BUNDLE_PLAYWRIGHT:-0}"

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) PBS_ARCH="x86_64"; PACKAGE_ARCH="x86_64" ;;
    aarch64|arm64) PBS_ARCH="aarch64"; PACKAGE_ARCH="aarch64" ;;
    *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

PBS_FILENAME="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${PBS_ARCH}-unknown-linux-gnu-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_FILENAME}"
PACKAGE_NAME="Row-Bot-${VERSION}-Linux-${PACKAGE_ARCH}"
PACKAGE_ROOT="$BUILD_DIR/$PACKAGE_NAME"
APP_SRC="$PACKAGE_ROOT/app"
PYTHON_PREFIX="$PACKAGE_ROOT/python"
TARBALL="$DIST_DIR/${PACKAGE_NAME}.tar.gz"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok() { echo -e "${GREEN}[  OK]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail() { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD} Build Row-Bot Linux package (v${VERSION})${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
info "Architecture: $ARCH ($PACKAGE_ARCH)"
info "Python:       $PYTHON_VERSION (PBS $PBS_RELEASE)"

# Node is a pinned build tool; none of its runtime is copied into the application.
CLIENT_BUILD="$PROJECT_DIR/frontend/dist"
CLIENT_NODE="$(command -v node)" || fail "Node 24.15.0 is required to stage the client"
[ "$("$CLIENT_NODE" --version)" = "v24.15.0" ] || fail "Client packaging requires Node 24.15.0"
for client_file in index.html asset-manifest.json .vite/manifest.json; do
    [ -f "$CLIENT_BUILD/$client_file" ] || fail "Client build missing; build and package the locked frontend first"
done

rm -rf "$PACKAGE_ROOT"
mkdir -p "$BUILD_DIR" "$DIST_DIR" "$APP_SRC" "$PACKAGE_ROOT/bin" \
         "$PACKAGE_ROOT/share/applications" "$PACKAGE_ROOT/share/icons/hicolor/256x256/apps"

PBS_TAR="$BUILD_DIR/$PBS_FILENAME"
if [ ! -f "$PBS_TAR" ]; then
    info "[1/6] Downloading Python $PYTHON_VERSION standalone ($PBS_ARCH)..."
    curl -L --fail -o "$PBS_TAR" "$PBS_URL"
    ok "Downloaded"
else
    info "[1/6] Using cached Python tarball"
fi

info "Extracting Python..."
rm -rf "$PYTHON_PREFIX"
mkdir -p "$PYTHON_PREFIX"
tar -xzf "$PBS_TAR" -C "$PYTHON_PREFIX" --strip-components=1
"$PYTHON_PREFIX/bin/python3" --version
ok "Python extracted"

if [ ! -f "$PROJECT_DIR/uv.lock" ]; then
    fail "uv.lock not found. Regenerate dependencies before building."
fi

info "[2/6] Installing locked Python packages from requirements.txt..."
"$PYTHON_PREFIX/bin/python3" -m pip install --upgrade pip setuptools wheel --quiet 2>&1 | tail -1 || true
"$PYTHON_PREFIX/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet 2>&1 | tail -5
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_runtime_dependencies.py" all
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_client_assets.py" --root "$CLIENT_BUILD"
ok "Python packages installed"

if [ "$PACKAGE_ARCH" = "x86_64" ]; then
    info "Checking native CPU baselines for older x86_64 compatibility..."
    "$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/check_linux_native_baseline.py" --arch "$PACKAGE_ARCH"
    ok "Native CPU baselines are package-compatible"
fi

if [ "$BUNDLE_PLAYWRIGHT" = "1" ]; then
    info "Installing Playwright Chromium into package..."
    export PLAYWRIGHT_BROWSERS_PATH="$PYTHON_PREFIX/playwright-browsers"
    "$PYTHON_PREFIX/bin/python3" -m playwright install chromium 2>&1 | tail -3 || \
        warn "Playwright Chromium install failed; Browser Automation requires an explicit Install or Repair action"
else
    info "Skipping bundled Playwright Chromium; Browser Automation will use a supported browser channel or an explicitly installed managed runtime"
fi

info "[3/6] Copying source code..."
MANIFEST_PY="$PROJECT_DIR/scripts/app_payload_manifest.py"
if [ ! -f "$MANIFEST_PY" ]; then
    fail "App payload manifest not found: $MANIFEST_PY"
fi

copy_payload_file() {
    local rel="$1"
    local src="$PROJECT_DIR/$rel"
    local dest="$APP_SRC/$rel"
    if [ ! -f "$src" ]; then
        fail "Manifest file is missing: $rel"
    fi
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
}

while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    copy_payload_file "$rel"
done < <(python3 "$MANIFEST_PY" --project-root "$PROJECT_DIR" --category root_python_files)

while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    copy_payload_file "$rel"
done < <(python3 "$MANIFEST_PY" --project-root "$PROJECT_DIR" --category root_files)

while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    copy_payload_file "$rel"
done < <(python3 "$MANIFEST_PY" --project-root "$PROJECT_DIR" --category runtime_script_files)

# Historical contract now owned by scripts/app_payload_manifest.py:
# for pkg in src/row_bot
while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    if [ -d "$PROJECT_DIR/$pkg" ]; then
        mkdir -p "$(dirname "$APP_SRC/$pkg")"
        rsync -a \
              --exclude='__pycache__' --exclude='*.pyc' \
              --exclude='node_modules' --exclude='.pytest_cache' \
              --exclude='/static/client-v2/***' \
              --exclude='tests' --exclude='test' --exclude='test-results' \
              --exclude='*.test.js' --exclude='*.spec.js' \
              --filter='- *.bak' --filter='- *.bak[0-9]*' \
              "$PROJECT_DIR/$pkg/" "$APP_SRC/$pkg/"
    else
        fail "Manifest directory is missing: $pkg"
    fi
done < <(python3 "$MANIFEST_PY" --project-root "$PROJECT_DIR" --category payload_dirs)

# The source copy excludes old generated assets; this destination must be fresh.
CLIENT_STAGE="$APP_SRC/src/row_bot/static/client-v2"
"$CLIENT_NODE" "$PROJECT_DIR/frontend/scripts/asset-manifest.mjs" "$CLIENT_BUILD" --package-dir "$CLIENT_STAGE"
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_client_assets.py" \
    --root "$CLIENT_STAGE" --compare "$CLIENT_BUILD" --strict

# Historical contract now owned by scripts/app_payload_manifest.py:
# for dir in static sounds bundled_skills tool_guides;
while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    if [ -d "$PROJECT_DIR/$dir" ]; then
        mkdir -p "$(dirname "$APP_SRC/$dir")"
        rsync -a --exclude='*.pyc' "$PROJECT_DIR/$dir/" "$APP_SRC/$dir/"
    else
        fail "Manifest asset directory is missing: $dir"
    fi
done < <(python3 "$MANIFEST_PY" --project-root "$PROJECT_DIR" --category asset_dirs)

LINUX_ICON_SOURCE=""
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    if [ -f "$PROJECT_DIR/$rel" ]; then
        LINUX_ICON_SOURCE="$rel"
        break
    fi
done < <(python3 "$MANIFEST_PY" --project-root "$PROJECT_DIR" --category linux_icon_candidates)
if [ -n "$LINUX_ICON_SOURCE" ]; then
    cp "$PROJECT_DIR/$LINUX_ICON_SOURCE" \
       "$PACKAGE_ROOT/share/icons/hicolor/256x256/apps/row-bot.png"
fi
ok "Source code copied"

info "[4/6] Creating launchers and install metadata..."
cat > "$PACKAGE_ROOT/bin/row-bot" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    TARGET="$(readlink "$SOURCE")"
    case "$TARGET" in
        /*) SOURCE="$TARGET" ;;
        *) SOURCE="$DIR/$TARGET" ;;
    esac
done
ROOT="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"
PYTHON="$ROOT/python/bin/python3"
APP_DIR="$ROOT/app"
DATA_DIR="${ROW_BOT_DATA_DIR:-$HOME/.row-bot}"

mkdir -p "$DATA_DIR"
export ROW_BOT_INSTALL_ROOT="$ROOT"
export PYTHONNOUSERSITE=1
export PYTHONIOENCODING=utf-8

BUNDLED_BROWSERS="$ROOT/python/playwright-browsers"
USER_BROWSERS="$DATA_DIR/playwright-browsers"
if [ -d "$BUNDLED_BROWSERS" ]; then
    export PLAYWRIGHT_BROWSERS_PATH="$BUNDLED_BROWSERS"
else
    mkdir -p "$USER_BROWSERS"
    export PLAYWRIGHT_BROWSERS_PATH="$USER_BROWSERS"
fi

cd "$APP_DIR"
if [ "$#" -eq 0 ]; then
    set -- --browser --no-tray
fi
exec "$PYTHON" launcher.py "$@"
LAUNCHER
chmod +x "$PACKAGE_ROOT/bin/row-bot"

cat > "$PACKAGE_ROOT/share/applications/ai.row-bot.RowBot.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Row-Bot
Comment=Local-first AI assistant
Exec=row-bot
Icon=row-bot
Terminal=false
Categories=Utility;Office;
StartupNotify=true
DESKTOP

cat > "$PACKAGE_ROOT/install_info.json" <<JSON
{
  "name": "Row-Bot",
  "version": "$VERSION",
  "platform": "linux",
  "arch": "$PACKAGE_ARCH",
  "install_kind": "xdg-user-tarball"
}
JSON

cat > "$PACKAGE_ROOT/install.sh" <<'INSTALL'
#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(SOURCE_DIR="$SOURCE_DIR" "$SOURCE_DIR/python/bin/python3" - <<'PY'
import json, os
from pathlib import Path
print(json.loads((Path(os.environ['SOURCE_DIR']) / 'install_info.json').read_text())['version'])
PY
)"
APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/row-bot"
RELEASES_DIR="$APP_HOME/releases"
TARGET="$RELEASES_DIR/$VERSION"
STAGING="$RELEASES_DIR/.installing-$VERSION-$$"
BIN_HOME="${HOME}/.local/bin"
DESKTOP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

mkdir -p "$RELEASES_DIR" "$BIN_HOME" "$DESKTOP_HOME" "$ICON_HOME"
rm -rf "$STAGING"
cp -a "$SOURCE_DIR" "$STAGING"
rm -rf "$TARGET"
mv "$STAGING" "$TARGET"

ln -sfn "releases/$VERSION" "$APP_HOME/current"
ln -sfn "$APP_HOME/current/bin/row-bot" "$BIN_HOME/row-bot"

cp "$TARGET/share/applications/ai.row-bot.RowBot.desktop" "$DESKTOP_HOME/ai.row-bot.RowBot.desktop"
sed -i "s|^Exec=.*|Exec=$BIN_HOME/row-bot|" "$DESKTOP_HOME/ai.row-bot.RowBot.desktop"
cp "$TARGET/share/icons/hicolor/256x256/apps/row-bot.png" "$ICON_HOME/row-bot.png" 2>/dev/null || true
chmod +x "$TARGET/bin/row-bot"
chmod +x "$TARGET/install.sh" "$TARGET/uninstall.sh" 2>/dev/null || true

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_HOME" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true

if [ "${ROW_BOT_SUPPRESS_INSTALL_PATH_HINT:-0}" != "1" ]; then
    LAUNCH_CMD="row-bot"
    case ":${PATH}:" in
        *":${BIN_HOME}:"*) ;;
        *)
            LAUNCH_CMD="$BIN_HOME/row-bot"
            echo "[WARN] $BIN_HOME is not on PATH. Run $BIN_HOME/row-bot now, or add this to your shell profile:"
            echo '       export PATH="$HOME/.local/bin:$PATH"'
            echo "       Open a new terminal after updating your profile."
            ;;
    esac

    echo "Row-Bot $VERSION installed. Run: $LAUNCH_CMD"
fi
INSTALL
chmod +x "$PACKAGE_ROOT/install.sh"

cat > "$PACKAGE_ROOT/uninstall.sh" <<'UNINSTALL'
#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/row-bot"
BIN_HOME="${HOME}/.local/bin"
DESKTOP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

rm -f "$BIN_HOME/row-bot"
rm -f "$DESKTOP_HOME/ai.row-bot.RowBot.desktop"
rm -f "$ICON_HOME/row-bot.png"
rm -rf "$APP_HOME"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_HOME" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true

echo "Row-Bot application files removed. User data in ~/.row-bot was left untouched."
UNINSTALL
chmod +x "$PACKAGE_ROOT/uninstall.sh"
ok "Launchers created"

info "[5/6] Cleaning package..."
find "$PACKAGE_ROOT" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$PACKAGE_ROOT" -name '*.pyc' -delete 2>/dev/null || true
find "$APP_SRC" \( -name '.pytest_cache' -o -name 'test-results' -o -name 'node_modules' \) -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/ensurepip 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/test 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/unittest/test 2>/dev/null || true
# Keep package-internal test dirs under site-packages:
# some runtime packages import private helpers from those modules.
if [ -d "$APP_SRC/docs" ]; then
    fail "Linux package payload unexpectedly contains docs/"
fi
if find "$APP_SRC" -path "$APP_SRC/scripts/verify_runtime_dependencies.py" -prune -o \
       \( -path '*/tests/*' -o -name 'test_*.py' -o -name '*_test.py' -o -name '*_harness.py' -o -name 'pytest.ini' \) -print -quit | grep -q .; then
    fail "Linux package payload contains test or harness artifacts"
fi
if find "$APP_SRC/scripts" -type f ! -name 'verify_runtime_dependencies.py' -print -quit | grep -q .; then
    fail "Linux package payload contains non-runtime scripts"
fi
find "$PACKAGE_ROOT" -name '*.so' -print0 | while IFS= read -r -d '' lib; do
    strip "$lib" 2>/dev/null || true
done
ok "Package cleaned"

info "Verifying assembled Linux runtime dependencies..."
ROW_BOT_INSTALL_ROOT="$PACKAGE_ROOT" PYTHONNOUSERSITE=1 \
    "$PYTHON_PREFIX/bin/python3" "$APP_SRC/scripts/verify_runtime_dependencies.py" all
ok "Assembled Linux runtime dependencies verified"
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_client_assets.py" \
    --root "$CLIENT_STAGE" --compare "$CLIENT_BUILD" --strict

info "[6/6] Creating tarball..."
rm -f "$TARBALL"
tar -C "$BUILD_DIR" -czf "$TARBALL" "$PACKAGE_NAME"
SIZE="$(du -sh "$TARBALL" | cut -f1)"
ok "Created $TARBALL ($SIZE)"

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN} Linux build complete${NC}"
echo -e "${GREEN} $TARBALL${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
