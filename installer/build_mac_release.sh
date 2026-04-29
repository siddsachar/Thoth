#!/usr/bin/env bash
# =============================================================================
# build_mac_release.sh — Package Thoth into a distributable macOS zip
#
# Creates:  installer/Thoth-<version>-macOS.zip
#
# The zip contains the full project directory with "Start Thoth.command"
# at the top level.  Users unzip, double-click the .command file, and
# everything is installed automatically.
#
# Usage:  ./installer/build_mac_release.sh
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
OUTPUT_NAME="Thoth-${VERSION}-macOS"
OUTPUT_ZIP="${OUTPUT_DIR:-$SCRIPT_DIR}/${OUTPUT_NAME}.zip"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[  OK]${NC}  $*"; }

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD} 𓂀  Build Thoth macOS Release Zip${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""

# ── Sanity checks ───────────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/Start Thoth.command" ]; then
    echo -e "${RED}[FAIL]${NC}  Start Thoth.command not found at project root."
    exit 1
fi

if [ ! -f "$PROJECT_DIR/requirements.txt" ]; then
    echo -e "${RED}[FAIL]${NC}  requirements.txt not found."
    exit 1
fi

# ── Ensure shell scripts are executable ─────────────────────────────────────
chmod +x "$PROJECT_DIR/Start Thoth.command"
chmod +x "$PROJECT_DIR/installer/Thoth.app/Contents/MacOS/thoth" 2>/dev/null || true
find "$PROJECT_DIR/installer" -name "*.sh" -exec chmod +x {} \;

# ── Build zip ───────────────────────────────────────────────────────────────
info "Building zip archive (excluding dev/runtime files)..."

# Create a staging directory named "Thoth" so the zip extracts to Thoth/
STAGING="$(mktemp -d)/Thoth"
rsync -a --exclude='.venv' \
         --exclude='__pycache__' \
         --exclude='.git' \
         --exclude='.github' \
         --exclude='dist' \
         --exclude='installer/build' \
         --exclude='installer/*.zip' \
         --exclude='installer/*.exe' \
         --exclude='.DS_Store' \
         --exclude='*.pyc' \
         --filter='- *.bak' \
         --filter='- *.bak[0-9]*' \
         --filter='- *.bak.*' \
         "$PROJECT_DIR/" "$STAGING/"

# Remove previous build
rm -f "$OUTPUT_ZIP"

cd "$(dirname "$STAGING")"
zip -r "$OUTPUT_ZIP" "Thoth"
rm -rf "$STAGING"

# ── Summary ─────────────────────────────────────────────────────────────────
ZIP_SIZE=$(du -h "$OUTPUT_ZIP" | cut -f1)
ok "Created $OUTPUT_ZIP ($ZIP_SIZE)"

echo ""
echo "  Contents:"
echo "    • Start Thoth.command   (double-click to install & launch)"
echo "    • Source files, requirements.txt, tools/, channels/"
echo "    • installer/Thoth.app/  (template, copied to /Applications at install)"
echo ""
echo "  Upload this zip to GitHub Releases for distribution."
echo ""
