#!/usr/bin/env bash
# =============================================================================
# build_linux_app.sh - Build self-contained Thoth Linux tarball
#
# Produces dist/Thoth-X.Y.Z-Linux-ARCH.tar.gz using python-build-standalone.
# The artifact installs into XDG user paths via install.sh and launches in
# browser/no-tray mode by default so Linux desktop system dependencies stay
# optional.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_VERSION="$(python3 - "$PROJECT_DIR/version.py" <<'PY'
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
PACKAGE_NAME="Thoth-${VERSION}-Linux-${PACKAGE_ARCH}"
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
echo -e "${BOLD} Build Thoth Linux package (v${VERSION})${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
info "Architecture: $ARCH ($PACKAGE_ARCH)"
info "Python:       $PYTHON_VERSION (PBS $PBS_RELEASE)"

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

info "[2/6] Installing Python packages from requirements.txt..."
"$PYTHON_PREFIX/bin/python3" -m pip install --upgrade pip setuptools wheel --quiet 2>&1 | tail -1 || true
"$PYTHON_PREFIX/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet 2>&1 | tail -5
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_runtime_dependencies.py" embeddings
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
        warn "Playwright Chromium install failed; browser tool will install on first use"
else
    info "Skipping bundled Playwright Chromium; using user browser cache at runtime"
fi

info "[3/6] Copying source code..."
for f in "$PROJECT_DIR"/*.py; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    case "$base" in
        workflows.py|seed_knowledge_graph.py) continue ;;
        test_*.py|test_suite.py|test_memory_e2e.py|integration_tests.py) continue ;;
        _*.py) continue ;;
    esac
    cp "$f" "$APP_SRC/"
done
cp "$PROJECT_DIR/requirements.txt" "$APP_SRC/"

for pkg in tools channels bundled_skills tool_guides ui plugins designer developer scripts utils providers mcp_client migration buddy; do
    if [ -d "$PROJECT_DIR/$pkg" ]; then
        rsync -a \
              --exclude='__pycache__' --exclude='*.pyc' \
              --filter='- *.bak' --filter='- *.bak[0-9]*' \
              "$PROJECT_DIR/$pkg/" "$APP_SRC/$pkg/"
    fi
done

for dir in static sounds docs; do
    if [ -d "$PROJECT_DIR/$dir" ]; then
        rsync -a --exclude='*.pyc' "$PROJECT_DIR/$dir/" "$APP_SRC/$dir/"
    fi
done

if [ -f "$PROJECT_DIR/thoth.ico" ]; then
    cp "$PROJECT_DIR/thoth.ico" "$APP_SRC/"
fi
if [ -f "$PROJECT_DIR/docs/thoth_glyph_256.png" ]; then
    cp "$PROJECT_DIR/docs/thoth_glyph_256.png" \
       "$PACKAGE_ROOT/share/icons/hicolor/256x256/apps/thoth.png"
elif [ -f "$PROJECT_DIR/docs/thoth_glyph.png" ]; then
    cp "$PROJECT_DIR/docs/thoth_glyph.png" \
       "$PACKAGE_ROOT/share/icons/hicolor/256x256/apps/thoth.png"
fi
ok "Source code copied"

info "[4/6] Creating launchers and install metadata..."
cat > "$PACKAGE_ROOT/bin/thoth" <<'LAUNCHER'
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
DATA_DIR="${THOTH_DATA_DIR:-$HOME/.thoth}"

mkdir -p "$DATA_DIR"
export THOTH_INSTALL_ROOT="$ROOT"
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
chmod +x "$PACKAGE_ROOT/bin/thoth"

cat > "$PACKAGE_ROOT/share/applications/com.thoth.Thoth.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Thoth
Comment=Local-first AI assistant
Exec=thoth
Icon=thoth
Terminal=false
Categories=Utility;Office;
StartupNotify=true
DESKTOP

cat > "$PACKAGE_ROOT/install_info.json" <<JSON
{
  "name": "Thoth",
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
APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/thoth"
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
ln -sfn "$APP_HOME/current/bin/thoth" "$BIN_HOME/thoth"

cp "$TARGET/share/applications/com.thoth.Thoth.desktop" "$DESKTOP_HOME/com.thoth.Thoth.desktop"
sed -i "s|^Exec=.*|Exec=$BIN_HOME/thoth|" "$DESKTOP_HOME/com.thoth.Thoth.desktop"
cp "$TARGET/share/icons/hicolor/256x256/apps/thoth.png" "$ICON_HOME/thoth.png" 2>/dev/null || true
chmod +x "$TARGET/bin/thoth"
chmod +x "$TARGET/install.sh" "$TARGET/uninstall.sh" 2>/dev/null || true

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_HOME" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true

if [ "${THOTH_SUPPRESS_INSTALL_PATH_HINT:-0}" != "1" ]; then
    LAUNCH_CMD="thoth"
    case ":${PATH}:" in
        *":${BIN_HOME}:"*) ;;
        *)
            LAUNCH_CMD="$BIN_HOME/thoth"
            echo "[WARN] $BIN_HOME is not on PATH. Run $BIN_HOME/thoth now, or add this to your shell profile:"
            echo '       export PATH="$HOME/.local/bin:$PATH"'
            echo "       Open a new terminal after updating your profile."
            ;;
    esac

    echo "Thoth $VERSION installed. Run: $LAUNCH_CMD"
fi
INSTALL
chmod +x "$PACKAGE_ROOT/install.sh"

cat > "$PACKAGE_ROOT/uninstall.sh" <<'UNINSTALL'
#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/thoth"
BIN_HOME="${HOME}/.local/bin"
DESKTOP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

rm -f "$BIN_HOME/thoth"
rm -f "$DESKTOP_HOME/com.thoth.Thoth.desktop"
rm -f "$ICON_HOME/thoth.png"
rm -rf "$APP_HOME"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_HOME" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true

echo "Thoth application files removed. User data in ~/.thoth was left untouched."
UNINSTALL
chmod +x "$PACKAGE_ROOT/uninstall.sh"
ok "Launchers created"

info "[5/6] Cleaning package..."
find "$PACKAGE_ROOT" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$PACKAGE_ROOT" -name '*.pyc' -delete 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/ensurepip 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/test 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/unittest/test 2>/dev/null || true
find "$PYTHON_PREFIX/lib" -type d -name 'tests' -exec rm -rf {} + 2>/dev/null || true
find "$PACKAGE_ROOT" -name '*.so' -print0 | while IFS= read -r -d '' lib; do
    strip "$lib" 2>/dev/null || true
done
ok "Package cleaned"

info "Verifying assembled Linux runtime dependencies..."
THOTH_INSTALL_ROOT="$PACKAGE_ROOT" PYTHONNOUSERSITE=1 \
    "$PYTHON_PREFIX/bin/python3" "$APP_SRC/scripts/verify_runtime_dependencies.py" embeddings
ok "Assembled Linux runtime dependencies verified"

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
