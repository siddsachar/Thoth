#!/usr/bin/env bash
# =============================================================================
# Start Thoth.command — macOS one-click installer & launcher
#
# Double-click this file in Finder to:
#   • First run:  Install dependencies, set up Thoth, then launch
#   • After that: Just launch Thoth (fast, ~3 seconds)
#
# Works on Apple Silicon (M1/M2/M3/M4) and Intel Macs.
# =============================================================================

set -euo pipefail

# ── Ensure Homebrew paths are available ──────────────────────────────────────
# When Finder launches a .command file, PATH is minimal and doesn't include
# Homebrew.  Add common Homebrew locations so we can find python3, brew, ollama.
for brew_prefix in /opt/homebrew /usr/local; do
    if [ -d "$brew_prefix/bin" ]; then
        export PATH="$brew_prefix/bin:$brew_prefix/sbin:$PATH"
    fi
done

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[  OK]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ── Resolve project root ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
VENV_DIR="$PROJECT_DIR/.venv"
THOTH_HOME="$HOME/.thoth"
OLLAMA_PORT=11434

# ── Is this a first-time install? ───────────────────────────────────────────
if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    # =====================================================================
    #  FAST PATH — already installed, just launch
    # =====================================================================
    source "$VENV_DIR/bin/activate"

    # Start Ollama if not running
    ollama_running() { (echo >/dev/tcp/127.0.0.1/$OLLAMA_PORT) 2>/dev/null; }

    if ! ollama_running; then
        OLLAMA_BIN=""
        for candidate in \
            "$(command -v ollama 2>/dev/null || true)" \
            "/usr/local/bin/ollama" \
            "/opt/homebrew/bin/ollama"; do
            if [ -n "$candidate" ] && [ -x "$candidate" ]; then
                OLLAMA_BIN="$candidate"
                break
            fi
        done
        if [ -n "$OLLAMA_BIN" ]; then
            "$OLLAMA_BIN" serve &>/dev/null &
            for _ in $(seq 1 30); do
                ollama_running && break
                sleep 0.5
            done
        fi
    fi

    cd "$PROJECT_DIR"
    exec python launcher.py
fi

# =====================================================================
#  INSTALL PATH — first time setup
# =====================================================================

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD} 𓁟  Thoth — macOS Setup${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo "  This will install Thoth and its dependencies."
echo "  It takes 5–15 minutes depending on your internet."
echo ""

# ── Helper: find a suitable Python ───────────────────────────────────────────
find_python() {
    PYTHON_CMD=""
    for cmd in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            PY_VERSION=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
            PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
            if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
                PYTHON_CMD="$cmd"
                return 0
            fi
        fi
    done
    return 1
}

# ── Helper: ensure Homebrew is installed ─────────────────────────────────────
ensure_brew() {
    if command -v brew &>/dev/null; then
        return 0
    fi
    echo ""
    warn "Homebrew is not installed. Thoth needs it to install Python and Ollama."
    echo ""
    echo -e "  ${BOLD}Homebrew${NC} is a free, widely-used macOS package manager."
    echo -e "  The installer will ask for your ${BOLD}Mac password${NC} and may take a few minutes."
    echo ""
    read -rp "  Install Homebrew now? [Y/n] " answer
    answer="${answer:-Y}"
    if [[ "$answer" =~ ^[Yy] ]]; then
        info "Installing Homebrew (this may take a few minutes)..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

        # Add Homebrew to PATH for this session
        for brew_prefix in /opt/homebrew /usr/local; do
            if [ -x "$brew_prefix/bin/brew" ]; then
                eval "$("$brew_prefix/bin/brew" shellenv)"
                break
            fi
        done

        if command -v brew &>/dev/null; then
            ok "Homebrew installed"
            return 0
        else
            fail "Homebrew installation failed. Please install manually:
       https://brew.sh"
        fi
    else
        return 1
    fi
}

# ── 1. Check Python ─────────────────────────────────────────────────────────
info "Checking Python..."

if find_python; then
    ok "Python $PY_VERSION ($PYTHON_CMD)"
else
    warn "Python 3.10+ not found. Attempting to install..."
    echo ""

    if ensure_brew; then
        info "Installing Python via Homebrew..."
        brew install python@3.12

        # Refresh PATH — include Homebrew's versioned & unversioned symlinks
        for brew_prefix in /opt/homebrew /usr/local; do
            if [ -d "$brew_prefix/bin" ]; then
                export PATH="$brew_prefix/bin:$PATH"
            fi
            # Homebrew puts unversioned 'python3' symlink here
            if [ -d "$brew_prefix/opt/python@3.12/libexec/bin" ]; then
                export PATH="$brew_prefix/opt/python@3.12/libexec/bin:$PATH"
            fi
        done

        if find_python; then
            ok "Python $PY_VERSION installed ($PYTHON_CMD)"
        else
            fail "Python installation failed.
       Please install Python 3.10+ manually from https://www.python.org/downloads/
       Then re-run this script."
        fi
    else
        fail "Python 3.10+ is required but not found.

       Install via Homebrew:  brew install python@3.12
       Or download from:      https://www.python.org/downloads/"
    fi
fi

# ── 2. Check / install Ollama ────────────────────────────────────────────────
info "Checking Ollama..."

if command -v ollama &>/dev/null; then
    OLLAMA_VER=$(ollama --version 2>/dev/null | head -1 || echo "installed")
    ok "Ollama already installed ($OLLAMA_VER)"
else
    if command -v brew &>/dev/null || ensure_brew; then
        info "Installing Ollama via Homebrew..."
        brew install ollama
        if command -v ollama &>/dev/null; then
            ok "Ollama installed"
        else
            warn "Homebrew install completed but 'ollama' not found on PATH."
            warn "You may need to restart your terminal."
        fi
    else
        echo ""
        warn "Ollama is not installed."
        echo ""
        echo -e "  ${BOLD}Download Ollama:${NC}  https://ollama.com/download/mac"
        echo ""
        warn "Continuing without Ollama — Thoth needs it to run language models."
    fi
fi

# ── 3. Create virtual environment ───────────────────────────────────────────
info "Creating Python virtual environment..."
"$PYTHON_CMD" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
ok "Virtual environment created"

# Upgrade pip
info "Upgrading pip..."
pip install --upgrade pip --quiet 2>&1
ok "pip upgraded"

# ── 4. Install Python packages ──────────────────────────────────────────────
info "Installing Python packages (this takes a few minutes)..."

REQUIREMENTS="$PROJECT_DIR/requirements.txt"
if [ ! -f "$REQUIREMENTS" ]; then
    fail "requirements.txt not found at $REQUIREMENTS"
fi

pip install -r "$REQUIREMENTS" 2>&1 | while IFS= read -r line; do
    # Show progress but keep it clean
    if [[ "$line" == *"Successfully installed"* ]] || [[ "$line" == *"Requirement already"* ]]; then
        echo "  $line"
    elif [[ "$line" == *"Installing collected"* ]]; then
        echo "  $line"
    fi
done
ok "Python packages installed"

# ── 5. Save project location ────────────────────────────────────────────────
mkdir -p "$THOTH_HOME"
echo "$PROJECT_DIR" > "$THOTH_HOME/thoth_home"
ok "Saved project location to ~/.thoth/thoth_home"

# ── 6. Set up Thoth.app ─────────────────────────────────────────────────────
info "Setting up Thoth.app..."

APP_DIR="$PROJECT_DIR/Thoth.app"
CONTENTS="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

mkdir -p "$MACOS_DIR" "$RESOURCES"

# Info.plist
cat > "$CONTENTS/Info.plist" << 'PLIST'
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
    <string>3.2.0</string>
    <key>CFBundleShortVersionString</key>
    <string>3.2.0</string>
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
    <string>Thoth uses the microphone for voice-to-text (speech recognition).</string>
</dict>
</plist>
PLIST

# Executable — reads ~/.thoth/thoth_home to find project
cat > "$MACOS_DIR/thoth" << 'APPSCRIPT'
#!/usr/bin/env bash
# Thoth.app launcher — finds the project via ~/.thoth/thoth_home

THOTH_HOME_FILE="$HOME/.thoth/thoth_home"

if [ ! -f "$THOTH_HOME_FILE" ]; then
    osascript -e 'display dialog "Thoth has not been set up yet.\n\nDouble-click \"Start Thoth.command\" first to install." buttons {"OK"} default button "OK" with icon stop with title "Thoth"' 2>/dev/null
    exit 1
fi

PROJECT_DIR="$(cat "$THOTH_HOME_FILE")"
VENV_DIR="$PROJECT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    osascript -e 'display dialog "Thoth virtual environment not found.\n\nRun \"Start Thoth.command\" to reinstall." buttons {"OK"} default button "OK" with icon stop with title "Thoth"' 2>/dev/null
    exit 1
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# Start Ollama if not running
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

cd "$PROJECT_DIR"
exec python launcher.py
APPSCRIPT
chmod +x "$MACOS_DIR/thoth"

ok "Thoth.app created"

# Offer to copy to /Applications
echo ""
echo -e "  ${CYAN}Would you like to copy Thoth.app to /Applications?${NC}"
echo -e "  This lets you launch Thoth from Spotlight, Launchpad, or Dock."
echo ""
read -r -p "  Copy to /Applications? [Y/n] " COPY_ANSWER
COPY_ANSWER=${COPY_ANSWER:-Y}

if [[ "$COPY_ANSWER" =~ ^[Yy]$ ]]; then
    if [ -d "/Applications/Thoth.app" ]; then
        rm -rf "/Applications/Thoth.app"
    fi
    cp -R "$APP_DIR" "/Applications/Thoth.app"
    ok "Copied to /Applications/Thoth.app"
    echo -e "    ${CYAN}Tip:${NC} Open Thoth from Spotlight (⌘+Space → type \"Thoth\")"
else
    echo -e "    ${CYAN}Tip:${NC} You can drag Thoth.app to your Dock anytime."
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${GREEN} ✓  Thoth installation complete!${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo "  Launching Thoth for the first time..."
echo "  (The default AI model will download on first chat — ~4 GB)"
echo ""
sleep 2

# Start Ollama
ollama_running() { (echo >/dev/tcp/127.0.0.1/$OLLAMA_PORT) 2>/dev/null; }
if ! ollama_running; then
    OLLAMA_BIN=""
    for candidate in \
        "$(command -v ollama 2>/dev/null || true)" \
        "/usr/local/bin/ollama" \
        "/opt/homebrew/bin/ollama"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            OLLAMA_BIN="$candidate"
            break
        fi
    done
    if [ -n "$OLLAMA_BIN" ]; then
        "$OLLAMA_BIN" serve &>/dev/null &
        for _ in $(seq 1 30); do
            ollama_running && break
            sleep 0.5
        done
    fi
fi

cd "$PROJECT_DIR"
exec python launcher.py
