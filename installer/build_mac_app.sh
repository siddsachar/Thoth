#!/usr/bin/env bash
# =============================================================================
# build_mac_app.sh — Build self-contained Thoth.app + .pkg installer
#
# Downloads python-build-standalone, installs all pip dependencies into it,
# assembles a proper macOS .app bundle, and optionally signs + creates .pkg.
#
# Usage:
#   ./installer/build_mac_app.sh                  # local unsigned build
#   ./installer/build_mac_app.sh 3.18.0            # specify version
#
# For signed builds (CI), set environment variables:
#   CODESIGN_IDENTITY="Developer ID Application: Name (TEAMID)"
#   PKG_SIGN_IDENTITY="Developer ID Installer: Name (TEAMID)"
#
# Tune the Python version / PBS release tag here or via env vars.
# Browse releases: https://github.com/indygreg/python-build-standalone/releases
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
BUILD_DIR="$SCRIPT_DIR/build/mac"
DIST_DIR="$PROJECT_DIR/dist"

# Signing identities — set by CI or leave empty for unsigned local builds
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:-}"       # Developer ID Application
PKG_SIGN_IDENTITY="${PKG_SIGN_IDENTITY:-}"       # Developer ID Installer
ENTITLEMENTS="$SCRIPT_DIR/entitlements.plist"

# Playwright browser bundling policy:
# - auto: bundle for local unsigned builds, skip for signed/notarized builds
# - 1:    force bundle Chromium
# - 0:    never bundle Chromium
BUNDLE_PLAYWRIGHT="${BUNDLE_PLAYWRIGHT:-auto}"
if [ "$BUNDLE_PLAYWRIGHT" = "auto" ]; then
    if [ -n "$CODESIGN_IDENTITY" ]; then
        BUNDLE_PLAYWRIGHT="0"
    else
        BUNDLE_PLAYWRIGHT="1"
    fi
fi

# Detect architecture
ARCH="$(uname -m)"
case "$ARCH" in
    arm64)  PBS_ARCH="aarch64" ;;
    x86_64) PBS_ARCH="x86_64" ;;
    *)      echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

PBS_FILENAME="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${PBS_ARCH}-apple-darwin-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_FILENAME}"

# App bundle layout
APP_BUNDLE="$BUILD_DIR/Thoth.app"
CONTENTS="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
APP_SRC="$RESOURCES/app"
PYTHON_PREFIX="$RESOURCES/python"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[  OK]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD} 𓂀  Build Thoth.app  (v${VERSION})${NC}"
echo -e "${BOLD}    Self-Contained macOS Installer${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
info "Architecture: $ARCH ($PBS_ARCH)"
info "Python:       $PYTHON_VERSION (PBS $PBS_RELEASE)"

# ── Clean previous build ────────────────────────────────────────────────────
rm -rf "$APP_BUNDLE"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

# ═════════════════════════════════════════════════════════════════════════════
#  1. Download python-build-standalone
# ═════════════════════════════════════════════════════════════════════════════
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

# ═════════════════════════════════════════════════════════════════════════════
#  2. Install pip packages
# ═════════════════════════════════════════════════════════════════════════════
info "[2/6] Installing Python packages from requirements.txt..."
"$PYTHON_PREFIX/bin/python3" -m pip install --upgrade pip setuptools wheel --quiet 2>&1 | tail -1 || true
"$PYTHON_PREFIX/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet 2>&1 | tail -5
ok "Python packages installed"

# Install Playwright Chromium into the app bundle when enabled.
# For signed/notarized builds we skip this because bundled Chromium fails notarization.
if [ "$BUNDLE_PLAYWRIGHT" = "1" ]; then
    info "Installing Playwright Chromium browser into app bundle..."
    export PLAYWRIGHT_BROWSERS_PATH="$PYTHON_PREFIX/playwright-browsers"
    "$PYTHON_PREFIX/bin/python3" -m playwright install chromium 2>&1 | tail -3 || \
        warn "Playwright Chromium install failed — browser tool will auto-install on first use"
    ok "Playwright Chromium installed"
else
    info "Skipping bundled Playwright Chromium for signed/notarized build"
fi

# ═════════════════════════════════════════════════════════════════════════════
#  3. Copy Thoth source code
# ═════════════════════════════════════════════════════════════════════════════
info "[3/6] Copying source code..."

mkdir -p "$MACOS_DIR" "$RESOURCES" "$APP_SRC"

# Core Python files at project root (exclude legacy/backup files)
for f in "$PROJECT_DIR"/*.py; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    case "$base" in
        workflows.py|seed_knowledge_graph.py) continue ;; # legacy, skip
        test_*.py|test_suite.py|test_memory_e2e.py|integration_tests.py) continue ;; # test files, skip
        _*.py) continue ;; # dev/temp scripts, skip
    esac
    cp "$f" "$APP_SRC/"
done
cp "$PROJECT_DIR/requirements.txt" "$APP_SRC/"

# Sub-packages (tools, channels, bundled_skills, tool_guides, ui, plugins, designer, scripts, utils, mcp_client, migration)
for pkg in tools channels bundled_skills tool_guides ui plugins designer scripts utils mcp_client migration; do
    if [ -d "$PROJECT_DIR/$pkg" ]; then
        rsync -a \
              --exclude='__pycache__' --exclude='*.pyc' \
              --filter='- *.bak' --filter='- *.bak[0-9]*' \
              "$PROJECT_DIR/$pkg/" "$APP_SRC/$pkg/"
    fi
done

# Static assets and data directories
for dir in static sounds docs; do
    if [ -d "$PROJECT_DIR/$dir" ]; then
        rsync -a --exclude='*.pyc' "$PROJECT_DIR/$dir/" "$APP_SRC/$dir/"
    fi
done

# Icons — generate .icns from PNG if not already present
if [ ! -f "$PROJECT_DIR/thoth.icns" ] && [ -f "$PROJECT_DIR/docs/thoth_glyph.png" ]; then
    info "Generating thoth.icns from thoth_glyph.png..."
    ICONSET_DIR="$BUILD_DIR/thoth.iconset"
    mkdir -p "$ICONSET_DIR"
    SRC_PNG="$PROJECT_DIR/docs/thoth_glyph.png"
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz "$SRC_PNG" --out "$ICONSET_DIR/icon_${sz}x${sz}.png" >/dev/null 2>&1
    done
    for sz in 32 64 128 256 512 1024; do
        half=$((sz / 2))
        sips -z $sz $sz "$SRC_PNG" --out "$ICONSET_DIR/icon_${half}x${half}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET_DIR" -o "$PROJECT_DIR/thoth.icns"
    ok "Generated thoth.icns"
fi
[ -f "$PROJECT_DIR/thoth.icns" ] && cp "$PROJECT_DIR/thoth.icns" "$RESOURCES/thoth.icns"
[ -f "$PROJECT_DIR/thoth.ico" ]  && cp "$PROJECT_DIR/thoth.ico" "$APP_SRC/"

ok "Source code copied"

# ═════════════════════════════════════════════════════════════════════════════
#  4. Create launcher and Info.plist
# ═════════════════════════════════════════════════════════════════════════════
info "[4/6] Creating launcher and Info.plist..."

cat > "$MACOS_DIR/thoth" << 'LAUNCHER'
#!/usr/bin/env bash
# Thoth.app launcher — uses the bundled Python, no system deps required

BUNDLE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$BUNDLE_DIR/Resources"
PYTHON="$RESOURCES/python/bin/python3"
APP_DIR="$RESOURCES/app"
DATA_DIR="$HOME/.thoth"

# Ensure data directory exists
mkdir -p "$DATA_DIR"

# Prefer bundled Playwright browsers when present; otherwise use a writable user path
BUNDLED_BROWSERS="$RESOURCES/python/playwright-browsers"
USER_BROWSERS="$DATA_DIR/playwright-browsers"
if [ -d "$BUNDLED_BROWSERS" ]; then
    export PLAYWRIGHT_BROWSERS_PATH="$BUNDLED_BROWSERS"
else
    mkdir -p "$USER_BROWSERS"
    export PLAYWRIGHT_BROWSERS_PATH="$USER_BROWSERS"
fi

# Try to start Ollama if installed (optional — cloud models work without it)
OLLAMA_PORT=11434
ollama_running() { (echo >/dev/tcp/127.0.0.1/$OLLAMA_PORT) 2>/dev/null; }
if ! ollama_running; then
    for candidate in \
        "$(command -v ollama 2>/dev/null || true)" \
        "/usr/local/bin/ollama" \
        "/opt/homebrew/bin/ollama"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            "$candidate" serve &>/dev/null &
            for _ in $(seq 1 30); do
                ollama_running && break
                sleep 0.5
            done
            break
        fi
    done
fi

cd "$APP_DIR"
exec "$PYTHON" launcher.py
LAUNCHER

chmod +x "$MACOS_DIR/thoth"

# Info.plist — VERSION is substituted from the outer script
cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Thoth</string>
    <key>CFBundleDisplayName</key>
    <string>Thoth</string>
    <key>CFBundleIdentifier</key>
    <string>com.thoth.assistant</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>thoth</string>
    <key>CFBundleIconFile</key>
    <string>thoth</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Thoth uses the microphone for voice-to-text input.</string>
    <key>NSCameraUsageDescription</key>
    <string>Thoth uses the camera for the vision tool.</string>
</dict>
</plist>
PLIST

ok "Launcher and Info.plist created"

# ═════════════════════════════════════════════════════════════════════════════
#  5. Strip unnecessary files to reduce bundle size
# ═════════════════════════════════════════════════════════════════════════════
info "[5/6] Cleaning up build artifacts..."

find "$APP_BUNDLE" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$APP_BUNDLE" -name '*.pyc' -delete 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/ensurepip 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/test 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/unittest/test 2>/dev/null || true
find "$PYTHON_PREFIX/lib" -type d -name 'tests' -exec rm -rf {} + 2>/dev/null || true

# Strip debug symbols from native libraries to reduce bundle size
info "Stripping debug symbols from shared libraries..."
STRIPPED_COUNT=0
find "$APP_BUNDLE" \( -name '*.so' -o -name '*.dylib' \) -print0 | while IFS= read -r -d '' lib; do
    strip -x "$lib" 2>/dev/null && STRIPPED_COUNT=$((STRIPPED_COUNT + 1)) || true
done
ok "Stripped debug symbols from shared libraries"

BUNDLE_SIZE=$(du -sh "$APP_BUNDLE" | cut -f1)
ok "Bundle size: $BUNDLE_SIZE"

# ═════════════════════════════════════════════════════════════════════════════
#  6. Code-sign and package
# ═════════════════════════════════════════════════════════════════════════════
info "[6/6] Packaging..."

if [ -n "$CODESIGN_IDENTITY" ]; then
    info "Code-signing with: $CODESIGN_IDENTITY"

    if [ ! -f "$ENTITLEMENTS" ]; then
        fail "Entitlements file not found: $ENTITLEMENTS"
    fi

    # Sign all .so and .dylib files first (inside-out)
    find "$APP_BUNDLE" \( -name '*.so' -o -name '*.dylib' \) | while read -r lib; do
        codesign --force --options runtime --timestamp \
                 --entitlements "$ENTITLEMENTS" \
                 --sign "$CODESIGN_IDENTITY" "$lib"
    done

    # Sign Mach-O executables (python3 binary, etc.)
    find "$APP_BUNDLE" -type f -perm +111 | while read -r f; do
        if file -b "$f" | grep -q "Mach-O"; then
            codesign --force --options runtime --timestamp \
                     --entitlements "$ENTITLEMENTS" \
                     --sign "$CODESIGN_IDENTITY" "$f"
        fi
    done

    # Sign the bundle itself
    codesign --force --options runtime --timestamp \
             --entitlements "$ENTITLEMENTS" \
             --sign "$CODESIGN_IDENTITY" "$APP_BUNDLE"

    # Verify
    codesign --verify --deep --strict "$APP_BUNDLE"
    ok ".app signed and verified"
else
    warn "CODESIGN_IDENTITY not set — skipping code signing"
fi

# ── Build .pkg installer ────────────────────────────────────────────────────
COMPONENT_PKG="$BUILD_DIR/Thoth-component.pkg"
FINAL_PKG="$DIST_DIR/Thoth-${VERSION}-macOS-${ARCH}.pkg"
PKG_ROOT="$BUILD_DIR/pkg-root"

rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT/Applications"
cp -R "$APP_BUNDLE" "$PKG_ROOT/Applications/Thoth.app"

pkgbuild --root "$PKG_ROOT" \
         --identifier "com.thoth.assistant.pkg" \
         --version "$VERSION" \
         --install-location "/" \
         "$COMPONENT_PKG"

if [ -n "$PKG_SIGN_IDENTITY" ]; then
    info "Signing .pkg with: $PKG_SIGN_IDENTITY"
    productbuild --sign "$PKG_SIGN_IDENTITY" \
                 --package "$COMPONENT_PKG" \
                 "$FINAL_PKG"
    ok ".pkg signed"
else
    productbuild --package "$COMPONENT_PKG" "$FINAL_PKG"
    warn ".pkg not signed (PKG_SIGN_IDENTITY not set)"
fi

# Clean up intermediate files
rm -f "$COMPONENT_PKG"
rm -rf "$PKG_ROOT"

PKG_SIZE=$(du -sh "$FINAL_PKG" | cut -f1)
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN} Build complete!${NC}"
echo -e "${GREEN} .pkg: $FINAL_PKG ($PKG_SIZE)${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
