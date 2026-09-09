#!/usr/bin/env bash
# =============================================================================
# build_mac_app.sh â€” Build self-contained Row-Bot.app + .pkg installer
#
# Downloads python-build-standalone, installs all pip dependencies into it,
# assembles a proper macOS .app bundle, and optionally signs + creates .pkg.
#
# Usage:
#   ./installer/build_mac_app.sh                  # local unsigned build
#   ./installer/build_mac_app.sh X.Y.Z             # specify version
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
BUILD_DIR="$SCRIPT_DIR/build/mac"
DIST_DIR="$PROJECT_DIR/dist"

# Signing identities â€” set by CI or leave empty for unsigned local builds
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
APP_BUNDLE="$BUILD_DIR/Row-Bot.app"
CONTENTS="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
APP_SRC="$RESOURCES/app"
PYTHON_PREFIX="$RESOURCES/python"

# â”€â”€ Colours â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[  OK]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD} ð“‚€  Build Row-Bot.app  (v${VERSION})${NC}"
echo -e "${BOLD}    Self-Contained macOS Installer${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
info "Architecture: $ARCH ($PBS_ARCH)"
info "Python:       $PYTHON_VERSION (PBS $PBS_RELEASE)"

# Node is a pinned build tool; none of its runtime is copied into the application.
CLIENT_BUILD="$PROJECT_DIR/frontend/dist"
CLIENT_NODE="$(command -v node)" || fail "Node 24.15.0 is required to stage the client"
[ "$("$CLIENT_NODE" --version)" = "v24.15.0" ] || fail "Client packaging requires Node 24.15.0"
for client_file in index.html asset-manifest.json .vite/manifest.json; do
    [ -f "$CLIENT_BUILD/$client_file" ] || fail "Client build missing; build and package the locked frontend first"
done

# â”€â”€ Clean previous build â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
rm -rf "$APP_BUNDLE"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  1. Download python-build-standalone
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
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

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  2. Install pip packages
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
if [ ! -f "$PROJECT_DIR/uv.lock" ]; then
    fail "uv.lock not found. Regenerate dependencies before building."
fi

info "[2/6] Installing locked Python packages from requirements.txt..."
"$PYTHON_PREFIX/bin/python3" -m pip install --upgrade pip setuptools wheel --quiet 2>&1 | tail -1 || true
"$PYTHON_PREFIX/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet 2>&1 | tail -5
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_runtime_dependencies.py" all
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_client_assets.py" --root "$CLIENT_BUILD"
ok "Python packages installed"

# Install Playwright Chromium into the app bundle when enabled.
# For signed/notarized builds we skip this because bundled Chromium fails notarization.
if [ "$BUNDLE_PLAYWRIGHT" = "1" ]; then
    info "Installing Playwright Chromium browser into app bundle..."
    export PLAYWRIGHT_BROWSERS_PATH="$PYTHON_PREFIX/playwright-browsers"
    "$PYTHON_PREFIX/bin/python3" -m playwright install chromium 2>&1 | tail -3 || \
        warn "Playwright Chromium install failed; Browser Automation requires an explicit Install or Repair action"
    ok "Playwright Chromium installed"
else
    info "Skipping bundled Playwright Chromium for signed/notarized build; Browser Automation setup remains an explicit user action"
fi

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  3. Copy Row-Bot source code
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
info "[3/6] Copying source code..."

mkdir -p "$MACOS_DIR" "$RESOURCES" "$APP_SRC"

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

# Static assets and data directories.
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

# Icons â€” generate .icns from PNG if not already present
MAC_ICON_SOURCE=""
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    if [ -f "$PROJECT_DIR/$rel" ]; then
        MAC_ICON_SOURCE="$rel"
        break
    fi
done < <(python3 "$MANIFEST_PY" --project-root "$PROJECT_DIR" --category mac_icon_source_candidates)
if [ ! -f "$PROJECT_DIR/row-bot.icns" ] && [ -n "$MAC_ICON_SOURCE" ]; then
    info "Generating row-bot.icns from $MAC_ICON_SOURCE..."
    ICONSET_DIR="$BUILD_DIR/row-bot.iconset"
    mkdir -p "$ICONSET_DIR"
    SRC_PNG="$PROJECT_DIR/$MAC_ICON_SOURCE"
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz "$SRC_PNG" --out "$ICONSET_DIR/icon_${sz}x${sz}.png" >/dev/null 2>&1
    done
    for sz in 32 64 128 256 512 1024; do
        half=$((sz / 2))
        sips -z $sz $sz "$SRC_PNG" --out "$ICONSET_DIR/icon_${half}x${half}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET_DIR" -o "$PROJECT_DIR/row-bot.icns"
    ok "Generated row-bot.icns"
fi
[ -f "$PROJECT_DIR/row-bot.icns" ] && cp "$PROJECT_DIR/row-bot.icns" "$RESOURCES/row-bot.icns"

ok "Source code copied"

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  4. Compile native launcher and create Info.plist
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
info "[4/6] Compiling native tray host and creating Info.plist..."

HOST_SOURCE="$SCRIPT_DIR/macos/RowBotTrayHost.m"
if [ ! -f "$HOST_SOURCE" ]; then
    fail "Native macOS tray host source missing: $HOST_SOURCE"
fi
if ! command -v xcrun >/dev/null 2>&1; then
    fail "xcrun not found. Install Xcode command line tools to build the macOS app."
fi

xcrun clang \
    -fobjc-arc \
    -fblocks \
    -framework Cocoa \
    -mmacosx-version-min=11.0 \
    "$HOST_SOURCE" \
    -o "$MACOS_DIR/row-bot"

chmod +x "$MACOS_DIR/row-bot"

# Info.plist â€” VERSION is substituted from the outer script
cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Row-Bot</string>
    <key>CFBundleDisplayName</key>
    <string>Row-Bot</string>
    <key>CFBundleIdentifier</key>
    <string>ai.row-bot.assistant</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>row-bot</string>
    <key>CFBundleIconFile</key>
    <string>row-bot</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Row-Bot uses the microphone for voice-to-text input.</string>
    <key>NSCameraUsageDescription</key>
    <string>Row-Bot uses the camera for the vision tool.</string>
</dict>
</plist>
PLIST

"$MACOS_DIR/row-bot" --self-test
ok "Native tray host and Info.plist created"

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  5. Strip unnecessary files to reduce bundle size
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
info "[5/6] Cleaning up build artifacts..."

find "$APP_BUNDLE" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$APP_BUNDLE" -name '*.pyc' -delete 2>/dev/null || true
find "$APP_SRC" \( -name '.pytest_cache' -o -name 'test-results' -o -name 'node_modules' \) -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/ensurepip 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/test 2>/dev/null || true
rm -rf "$PYTHON_PREFIX/lib/python"*/unittest/test 2>/dev/null || true
# Keep package-internal test dirs under site-packages:
# some runtime packages import private helpers from those modules.
if [ -d "$APP_SRC/docs" ]; then
    fail "macOS app payload unexpectedly contains docs/"
fi
if find "$APP_SRC" -path "$APP_SRC/scripts/verify_runtime_dependencies.py" -prune -o \
       \( -path '*/tests/*' -o -name 'test_*.py' -o -name '*_test.py' -o -name '*_harness.py' -o -name 'pytest.ini' \) -print -quit | grep -q .; then
    fail "macOS app payload contains test or harness artifacts"
fi
if find "$APP_SRC/scripts" -type f ! -name 'verify_runtime_dependencies.py' -print -quit | grep -q .; then
    fail "macOS app payload contains non-runtime scripts"
fi

# Strip debug symbols from native libraries to reduce bundle size
info "Stripping debug symbols from shared libraries..."
STRIPPED_COUNT=0
find "$APP_BUNDLE" \( -name '*.so' -o -name '*.dylib' \) -print0 | while IFS= read -r -d '' lib; do
    strip -x "$lib" 2>/dev/null && STRIPPED_COUNT=$((STRIPPED_COUNT + 1)) || true
done
ok "Stripped debug symbols from shared libraries"

BUNDLE_SIZE=$(du -sh "$APP_BUNDLE" | cut -f1)
ok "Bundle size: $BUNDLE_SIZE"

info "Verifying assembled app runtime dependencies..."
ROW_BOT_INSTALL_ROOT="$RESOURCES" PYTHONNOUSERSITE=1 \
    "$PYTHON_PREFIX/bin/python3" "$APP_SRC/scripts/verify_runtime_dependencies.py" all
ok "Assembled app runtime dependencies verified"
"$PYTHON_PREFIX/bin/python3" "$PROJECT_DIR/scripts/verify_client_assets.py" \
    --root "$CLIENT_STAGE" --compare "$CLIENT_BUILD" --strict

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  6. Code-sign and package
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
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
    warn "CODESIGN_IDENTITY not set â€” skipping code signing"
fi

# â”€â”€ Build .pkg installer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
COMPONENT_PKG="$BUILD_DIR/Row-Bot-component.pkg"
COMPONENT_PLIST="$BUILD_DIR/Row-Bot-component.plist"
FINAL_PKG="$DIST_DIR/Row-Bot-${VERSION}-macOS-${ARCH}.pkg"
PKG_ROOT="$BUILD_DIR/pkg-root"

rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT"
cp -R "$APP_BUNDLE" "$PKG_ROOT/Row-Bot.app"

cat > "$COMPONENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array>
  <dict>
    <key>BundleHasStrictIdentifier</key>
    <true/>
    <key>BundleIsRelocatable</key>
    <false/>
    <key>BundleIsVersionChecked</key>
    <true/>
    <key>BundleOverwriteAction</key>
    <string>upgrade</string>
    <key>RootRelativeBundlePath</key>
    <string>Row-Bot.app</string>
  </dict>
</array>
</plist>
PLIST

pkgbuild --root "$PKG_ROOT" \
         --component-plist "$COMPONENT_PLIST" \
         --identifier "ai.row-bot.assistant.pkg" \
         --version "$VERSION" \
         --install-location "/Applications" \
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
rm -f "$COMPONENT_PKG" "$COMPONENT_PLIST"
rm -rf "$PKG_ROOT"

PKG_SIZE=$(du -sh "$FINAL_PKG" | cut -f1)
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN} Build complete!${NC}"
echo -e "${GREEN} .pkg: $FINAL_PKG ($PKG_SIZE)${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
